"""DeckMate 后端服务：FastAPI + WebSocket 广播 + LLM 对话"""
from __future__ import annotations

import asyncio
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


CFG = load_config()
TOKEN = CFG["server"]["token"]

llm = LLMClient(
    base_url=CFG["llm"]["base_url"],
    api_key=CFG["llm"]["api_key"],
    model=CFG["llm"]["model"],
    timeout=CFG["llm"].get("timeout", 60),
)

SYSTEM_PROMPT = CFG["agent"]["system_prompt"]
HISTORY_LIMIT = int(CFG["agent"].get("history_limit", 20))

# 内存中的对话历史（单会话，MVP）
history: list[dict] = []


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


async def handle_chat(text: str) -> str:
    await manager.broadcast({"type": "user_message", "text": text, "ts": time.time()})
    history.append({"role": "user", "content": text})
    del history[:-HISTORY_LIMIT]

    if not llm.configured:
        reply = "LLM 未配置 API Key。请编辑 Deck 上的 ~/deckmate/server/config.toml 填入 api_key 后重启服务。"
    else:
        try:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + history
            reply = await llm.chat(msgs)
        except Exception as e:
            reply = f"LLM 调用失败：{e}"

    history.append({"role": "assistant", "content": reply})
    await manager.broadcast({"type": "assistant_message", "text": reply, "ts": time.time()})
    return reply


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
