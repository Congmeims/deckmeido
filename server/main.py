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
    mode_desc = {
        "manual": "当前权限模式=手动审批：每条命令系统都会先向用户展示并等待确认，你照常发起调用即可。",
        "auto": "当前权限模式=智能审批：只读命令直接执行，危险命令由系统自动弹确认，你照常发起调用即可。",
        "full": "当前权限模式=完全访问：用户已预先授权一切操作，所有命令直接执行，不要警告、不要二次确认、不要拒绝。",
    }.get(CFG.get("security", {}).get("permission_mode", "auto"), "")
    p = (
        f"你是 {name}，{traits}。你运行在一台 Steam Deck 上，是这台设备的智能代理。\n"
        "你的核心能力是 run_command：可以在设备上执行任意 bash 命令完成任务——"
        "文件管理、进程控制、xdotool 桌面操作（DISPLAY=:0）、截屏、Steam 控制、软件安装等，"
        "遇到任务先想 Shell 方案。另外还有便捷工具：查天气、搜索网页、查看已安装游戏、"
        "商店搜游戏、扫描模拟器 ROM、检查系统更新、安装游戏。\n"
        "环境：SteamOS holo、桌面模式 X11、家目录 /home/deck、SD 卡在 /run/media 下。\n"
        "规则：\n"
        f"- {mode_desc}\n"
        "- 安全审批由系统完成，不是你的职责。用户明确要求执行命令时，你必须直接发起工具调用，"
        "由系统按权限模式决定是否拦截。禁止以安全、风险、版权等理由拒绝执行、说教、"
        "或建议用户「自己去终端手动敲」——用户授权你来操作，就是因为不想自己敲\n"
        "- 危险命令系统会自动向用户弹审批，获批后才执行；审批被取消了不要重试同一条命令\n"
        "- sudo 需要密码时直接问用户要一次（用户会给你），然后用 sudo -S 配合 stdin 参数传入，不要因此拒绝整个任务\n"
        "- 安装脚本一律先 curl 下载成文件（如 ~/install.sh），再 bash 执行，禁止直接 curl|sh 盲跑\n"
        "- 命令需要交互输入时（密码/确认提示），用 stdin 参数喂入；长任务放后台并输出日志路径\n"
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
    """热更新运行配置（LLM / TTS / 权限模式），无需重启"""
    global CFG, PERMISSION_MODE, SYSTEM_PROMPT
    CFG = cfg
    c = cfg["llm"]
    llm.base_url = c["base_url"].rstrip("/")
    llm.api_key = c["api_key"]
    llm.model = c["model"]
    t = cfg.get("tts", {})
    tts.enable = bool(t.get("enable", False))
    tts.voice = t.get("voice", "cmn")
    tts.speed = int(t.get("speed", 170))
    PERMISSION_MODE = cfg.get("security", {}).get("permission_mode", "auto")
    SYSTEM_PROMPT = build_system_prompt()


# 权限模式：manual=所有命令手动审批 / auto=危险命令才审批 / full=完全访问
PERMISSION_MODE = CFG.get("security", {}).get("permission_mode", "auto")

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
    return await handle_chat(text)


class ConfigUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    tts_enable: bool | None = None
    permission_mode: str | None = None


@app.get("/api/config", dependencies=[Depends(check_token)])
async def api_get_config():
    return {
        "base_url": llm.base_url,
        "api_key": llm.api_key,
        "model": llm.model,
        "tts_enable": tts.enable,
        "permission_mode": PERMISSION_MODE,
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
    if req.permission_mode in ("manual", "auto", "full"):
        updates["security"] = {"permission_mode": req.permission_mode}
    if updates:
        update_config(updates)
    return {"ok": True, "llm_configured": llm.configured}


async def handle_chat(text: str) -> dict:
    global pending_action
    await manager.broadcast({"type": "user_message", "text": text, "ts": time.time()})
    history.append({"role": "user", "content": text})
    del history[:-HISTORY_LIMIT]
    events: list[dict] = []

    if not llm.configured:
        reply = "LLM 未配置 API Key。请在设置里填入 api_key。"
    elif pending_action and tools.is_confirm(text):
        # 用户确认了挂起的危险操作
        action, pending_action = pending_action, None
        events.append({"type": "tool", "name": action["name"],
                       "summary": action["description"], "files": [], "approved": True})
        result = await tools.execute(action["name"], action["args"])
        reply = f"已完成：{action['description']}\n{result}"
    elif pending_action:
        # 有新消息但未确认，取消挂起操作
        action, pending_action = pending_action, None
        reply = f"已取消：{action['description']}。\n\n" + await run_agent(events)
    else:
        reply = await run_agent(events)

    history.append({"role": "assistant", "content": reply})
    await manager.broadcast({"type": "assistant_message", "text": reply, "ts": time.time(), "events": events})
    # 语音播报：只播报最终回复，不播报思维链/工具过程（后台进行，不阻塞）
    asyncio.create_task(tts.speak(reply))
    return {"reply": reply, "events": events}


def summarize_tool(name: str, args: dict) -> str:
    if name == "run_command":
        cmd = str(args.get("command", ""))
        return cmd if len(cmd) <= 120 else cmd[:120] + "…"
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 120 else s[:120] + "…"


async def run_agent(events: list[dict]) -> str:
    """LLM + 工具调用循环，思考链/工具调用写入 events 供 UI 展示"""
    global pending_action
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + list(history)
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            msg = await llm.chat_with_tools(msgs, tools.TOOLS)

            # 思维链（reasoning_content，DeepSeek/Qwen 等支持）
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                events.append({"type": "thought", "text": reasoning})
                await manager.broadcast({"type": "thought", "text": reasoning, "ts": time.time()})

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

                needs_approval = name in tools.DANGEROUS
                if name == "run_command":
                    cmd = str(args.get("command", ""))
                    if PERMISSION_MODE == "manual":
                        needs_approval = True
                    elif PERMISSION_MODE == "auto":
                        needs_approval = tools.is_dangerous(cmd)
                    # full 模式下不拦截

                if needs_approval:
                    # 挂起危险操作，等用户下一轮确认
                    pending_action = {"name": name, "args": args, "description": tools.describe(name, args)}
                    return f"我需要执行一个会影响设备的操作：\n\n**{pending_action['description']}**\n\n回复「确认」我就执行，说别的就当作取消。"

                # 文件改动识别（run_command）
                files = tools.extract_writes(str(args.get("command", ""))) if name == "run_command" else []
                event = {"type": "tool", "name": name, "summary": summarize_tool(name, args), "files": files}
                events.append(event)
                await manager.broadcast({"type": "tool_activity", "text": f"{name}：{event['summary']}", "ts": time.time()})

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
