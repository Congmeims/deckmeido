"""DeckMate 后端服务：FastAPI + WebSocket 广播 + LLM 对话"""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import tomllib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Header, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .llm import LLMClient
from . import tools
from .tts import TTS

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
STATIC_DIR = BASE_DIR / "static"


# ---------- 配置 ----------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        shutil.copy(BASE_DIR / "config.example.toml", CONFIG_PATH)
    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # token 为空则生成并写回配置文件，方便用户查看
    if not cfg["server"].get("token"):
        token = secrets.token_urlsafe(16)
        text = CONFIG_PATH.read_text(encoding="utf-8")
        text = text.replace('token = ""', f'token = "{token}"', 1)
        CONFIG_PATH.write_text(text, encoding="utf-8")
        cfg["server"]["token"] = token
    return cfg


def update_config(updates: dict):
    """把 {section: {key: value}} 写回 config.toml（保留注释），并重载运行配置"""
    import re

    text = CONFIG_PATH.read_text(encoding="utf-8")
    for section, kv in updates.items():
        for key, value in kv.items():
            if isinstance(value, bool):
                v = "true" if value else "false"
            elif isinstance(value, (int, float)):
                v = str(value)
            else:
                v = '"' + str(value).replace('"', '\\"') + '"'
            # 在目标 section 内替换 key = ...，没有则在 section 末尾追加
            sec_pat = re.compile(rf"(^\[{re.escape(section)}\]\n(?:[^\[\n].*\n?)*)", re.M)
            m = sec_pat.search(text)
            kv_line = f"{key} = {v}"
            if m:
                block = m.group(1)
                if re.search(rf"^{re.escape(key)}\s*=", block, re.M):
                    new_block = re.sub(rf"^{re.escape(key)}\s*=.*$", kv_line, block, flags=re.M)
                else:
                    new_block = block.rstrip("\n") + "\n" + kv_line + "\n"
                text = text[: m.start()] + new_block + text[m.end():]
            else:
                text = text.rstrip("\n") + f"\n\n[{section}]\n{kv_line}\n"
    CONFIG_PATH.write_text(text, encoding="utf-8")
    apply_config(tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")))


CFG = load_config()
TOKEN = CFG["server"]["token"]

llm = LLMClient(
    base_url=CFG["llm"]["base_url"],
    api_key=CFG["llm"]["api_key"],
    model=CFG["llm"]["model"],
    timeout=CFG["llm"].get("timeout", 60),
)

PERSONA = CFG.get("persona", {})
HISTORY_LIMIT = int(CFG["agent"].get("history_limit", 20))
MAX_TOOL_ROUNDS = int(CFG["agent"].get("max_tool_rounds", 5))


def build_system_prompt() -> str:
    name = PERSONA.get("name", "DeckMate")
    traits = PERSONA.get("traits", "")
    extra = PERSONA.get("extra", "")
    p = (
        f"你是 {name}，{traits}。你运行在一台 Steam Deck 上，是这台设备的智能代理。\n"
        "你可以调用工具来完成任务：查天气、搜索网页、查看已安装游戏、在商店搜索游戏、"
        "扫描模拟器 ROM 收藏（list_emulator_roms）、检查系统更新、下载安装游戏。\n"
        "规则：\n"
        "- 涉及真实变更的操作（如安装游戏）会先向用户描述，等用户确认后才执行\n"
        "- 回答简洁，中文优先，保持人设语气\n"
        "- 需要 appid 时先用 find_game 查询，不要编造"
    )
    if extra:
        p += f"\n补充人设：{extra}"
    return p


SYSTEM_PROMPT = build_system_prompt()

_tts_cfg = CFG.get("tts", {})
tts = TTS(
    enable=bool(_tts_cfg.get("enable", False)),
    engine=_tts_cfg.get("engine", "espeak-ng"),
    voice=_tts_cfg.get("voice", "cmn"),
    speed=int(_tts_cfg.get("speed", 170)),
    max_chars=int(_tts_cfg.get("max_chars", 300)),
)


def apply_config(cfg: dict):
    """热更新运行配置（LLM / TTS），无需重启"""
    global CFG
    CFG = cfg
    c = cfg["llm"]
    llm.base_url = c["base_url"].rstrip("/")
    llm.api_key = c["api_key"]
    llm.model = c["model"]
    t = cfg.get("tts", {})
    tts.enable = bool(t.get("enable", False))
    tts.voice = t.get("voice", "cmn")
    tts.speed = int(t.get("speed", 170))

# 内存中的对话历史（单会话，MVP）
history: list[dict] = []

# 待确认的危险操作 {name, args, description}
pending_action: dict | None = None


# ---------- WebSocket 连接管理 ----------

class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# ---------- 认证 ----------

def check_token(token: str | None = Query(default=None), x_token: str | None = Header(default=None)):
    if token != TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


# ---------- 系统状态 ----------

def read_first(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def get_status() -> dict:
    battery = read_first("/sys/class/power_supply/BAT1/capacity")
    charging = read_first("/sys/class/power_supply/BAT1/status")
    load = ""
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
        load = " ".join(load)
    except OSError:
        pass
    gaming = subprocess.run(["pgrep", "-x", "gamescope"], capture_output=True).returncode == 0
    return {
        "battery": int(battery) if battery.isdigit() else None,
        "charging": charging or None,
        "load": load or None,
        "mode": "gaming" if gaming else "desktop",
        "llm_configured": llm.configured,
        "model": llm.model,
    }


# ---------- API ----------

app = FastAPI(title="DeckMate")


class ChatRequest(BaseModel):
    message: str


@app.get("/api/status", dependencies=[Depends(check_token)])
async def api_status():
    return get_status()


@app.post("/api/chat", dependencies=[Depends(check_token)])
async def api_chat(req: ChatRequest):
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    reply = await handle_chat(text)
    return {"reply": reply}


class ConfigUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    tts_enable: bool | None = None


@app.get("/api/config", dependencies=[Depends(check_token)])
async def api_get_config():
    return {
        "base_url": llm.base_url,
        "api_key": llm.api_key,
        "model": llm.model,
        "tts_enable": tts.enable,
    }


@app.post("/api/config", dependencies=[Depends(check_token)])
async def api_set_config(req: ConfigUpdate):
    llm_updates = {}
    if req.base_url is not None:
        llm_updates["base_url"] = req.base_url.strip()
    if req.api_key is not None:
        llm_updates["api_key"] = req.api_key.strip()
    if req.model is not None:
        llm_updates["model"] = req.model.strip()
    updates = {}
    if llm_updates:
        updates["llm"] = llm_updates
    if req.tts_enable is not None:
        updates["tts"] = {"enable": req.tts_enable}
    if updates:
        update_config(updates)
    return {"ok": True, "llm_configured": llm.configured}


async def handle_chat(text: str) -> str:
    global pending_action
    await manager.broadcast({"type": "user_message", "text": text, "ts": time.time()})
    history.append({"role": "user", "content": text})
    del history[:-HISTORY_LIMIT]

    if not llm.configured:
        reply = "LLM 未配置 API Key。请编辑 Deck 上的 ~/deckmate/server/config.toml 填入 api_key 后重启服务。"
    elif pending_action and tools.is_confirm(text):
        # 用户确认了挂起的危险操作
        action, pending_action = pending_action, None
        await manager.broadcast({"type": "tool_activity", "text": f"正在执行：{action['description']}", "ts": time.time()})
        result = await tools.execute(action["name"], action["args"])
        reply = f"已完成：{action['description']}\n{result}"
    elif pending_action:
        # 有新消息但未确认，取消挂起操作
        action, pending_action = pending_action, None
        reply = f"已取消：{action['description']}。\n\n" + await run_agent()
    else:
        reply = await run_agent()

    history.append({"role": "assistant", "content": reply})
    await manager.broadcast({"type": "assistant_message", "text": reply, "ts": time.time()})
    # 语音播报（后台进行，不阻塞回复）
    asyncio.create_task(tts.speak(reply))
    return reply


async def run_agent() -> str:
    """LLM + 工具调用循环"""
    global pending_action
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + list(history)
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            msg = await llm.chat_with_tools(msgs, tools.TOOLS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content") or "（没有回复内容）"

            msgs.append(msg)
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                if name in tools.DANGEROUS:
                    # 挂起危险操作，等用户下一轮确认
                    pending_action = {"name": name, "args": args, "description": tools.describe(name, args)}
                    return f"我需要执行一个会影响设备的操作：\n\n**{pending_action['description']}**\n\n回复「确认」我就执行，说别的就当作取消。"

                await manager.broadcast({"type": "tool_activity", "text": f"调用工具 {name}", "ts": time.time()})
                result = await tools.execute(name, args)
                msgs.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        return "（工具调用次数超限，换个问法试试吧）"
    except Exception as e:
        return f"LLM 调用失败：{e}"


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(default="")):
    if token != TOKEN:
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        # 新连接同步历史
        await ws.send_json({"type": "history", "messages": history})
        await ws.send_json({"type": "status", "data": get_status()})
        while True:
            data = await ws.receive_json()
            if data.get("type") == "chat":
                await handle_chat(str(data.get("text", "")).strip())
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# 静态网页控制台（放最后挂载，避免覆盖 API 路由）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
