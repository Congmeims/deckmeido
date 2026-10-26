"""DeckMeido 的 Decky 插件后端。

插件以 root 身份加载（见 plugin.json 的 flags），只做三件事：
1. 从 gamescope 的 PipeWire 节点抓一帧当前游戏画面
2. 连本机 :18765 的 /ws，把这轮问答的流式 token 转发给 QAM
3. 告诉 QAM 自己能不能用（后端在不在、token 读不读得到）

推理逻辑一行都不在这里：全部复用 DeckMeido 已有的后端，
插件只是"截图 + 转发 + 显示"。
"""

import asyncio
import base64
import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import decky


def _find_install_dir() -> Path:
    """找 DeckMeido 的安装目录（里面要有 server/config.toml）。

    插件自己装在 ~/homebrew/plugins/DeckMeido，和后端不是同一个目录，
    所以不能靠 __file__ 往上推，只能按已知位置挨个试。
    """
    homes = ["/home/deck"]
    try:
        # root 下 Path.home() 是 /root，这里枚举 home 目录下的真实用户
        homes += [str(p) for p in Path("/home").iterdir() if p.is_dir()]
    except Exception:
        pass
    for home in dict.fromkeys(homes):
        for name in ("deckmeido", "DeckMeido", "SDagent", "sdagent"):
            cand = Path(home) / name
            if (cand / "server" / "config.toml").exists():
                return cand
    return Path("/home/deck/deckmeido")


INSTALL_DIR = _find_install_dir()
CONFIG_PATH = INSTALL_DIR / "server" / "config.toml"


def _load_server_cfg() -> dict:
    """读后端的 [server] 段，拿 port 和 token"""
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("server") or {}
    except Exception:
        return {}


# 游戏模式下抓帧必须站在 gamescope 所属的普通用户那边做（root 连不上它的
# PipeWire socket），所以这里的命令一律以 deck 用户身份跑。
_RUN_AS: list[str] = []


def _init_run_as() -> list[str]:
    global _RUN_AS
    if _RUN_AS:
        return _RUN_AS
    if os.getuid() != 0:
        _RUN_AS = []  # 已经是普通用户，直接跑
        return _RUN_AS
    for user in ("deck",):
        if shutil.which("runuser"):
            _RUN_AS = ["runuser", "-u", user, "--"]
            return _RUN_AS
        if shutil.which("sudo"):
            _RUN_AS = ["sudo", "-u", user, "--"]
            return _RUN_AS
    return _RUN_AS


def _user_runtime_dir() -> str:
    """gamescope 的 PipeWire socket 在 /run/user/<uid> 下"""
    try:
        uid = Path("/home/deck").stat().st_uid
        return f"/run/user/{uid}"
    except Exception:
        return "/run/user/1000"


async def _gamescope_pw_node() -> str | None:
    """找 gamescope 的 PipeWire 视频源节点 id"""
    cmd = _init_run_as() + ["pw-dump"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "XDG_RUNTIME_DIR": _user_runtime_dir()},
        )
        out, _ = await proc.communicate()
        nodes = json.loads(out or b"[]")
        for o in nodes:
            props = (o.get("info") or {}).get("props") or {}
            if (
                props.get("node.name") == "gamescope"
                and props.get("media.class") == "Video/Source"
            ):
                return str(o.get("id"))
    except Exception as exc:
        decky.logger.warning(f"pw-dump 失败：{exc}")
        return None
    return None


async def grab_screenshot() -> tuple[bool, str]:
    """抓一帧游戏画面，返回 (成功, base64 或错误说明)

    写法和 server/tools.py 的 _screenshot_gamescope 保持一致：连抓 6 帧覆盖
    同一个文件，拿到的是最后一帧（避免拿到刚启动时的黑帧）。
    """
    node = await _gamescope_pw_node()
    if not node:
        return False, "没找到 gamescope 的画面节点：可能不在游戏模式，或后端没在游戏里跑过"

    gst = shutil.which("gst-launch-1.0")
    if not gst:
        # root 的 PATH 有时不含 /usr/bin，退一步找绝对路径
        gst = "/usr/bin/gst-launch-1.0"
        if not Path(gst).exists():
            return False, "机器上没有 gst-launch-1.0（gst-plugins-base 未安装）"

    tmp = Path("/tmp") / f"deckmeido_shot_{os.getpid()}.png"
    cmd = _init_run_as() + [
        gst, "-q", f"pipewiresrc path={node} num-buffers=6", "!",
        "videoconvert", "!", "pngenc", "!", f"filesink location={tmp}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": _user_runtime_dir()},
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=25)
        if not tmp.exists() or tmp.stat().st_size < 10000:
            detail = (err or b"").decode("utf-8", "replace").strip()[-300:]
            return False, f"抓帧没拿到有效画面{('：' + detail) if detail else ''}"
        data = base64.b64encode(tmp.read_bytes()).decode("ascii")
        return True, data
    except asyncio.TimeoutError:
        return False, "抓帧超时（25s）"
    except Exception as exc:
        return False, f"抓帧出错：{exc}"
    finally:
        tmp.unlink(missing_ok=True)


class Plugin:
    async def get_status(self) -> dict:
        """QAM 打开时先问一次：后端连得上吗、截图能不能用"""
        cfg = _load_server_cfg()
        port = cfg.get("port", 18765)
        node = await _gamescope_pw_node()
        return {
            "install_dir": str(INSTALL_DIR),
            "config_ok": CONFIG_PATH.exists(),
            "port": port,
            # token 交给前端连 :18765 用。QAM 本来就在这台机器上，
            # 而能读 config.toml 的人早就已经是 deck 用户了，不算多暴露一层。
            "token": str(cfg.get("token", "")),
            "gamescope": node is not None,
            "capture_ok": node is not None,
            "uid": os.getuid(),
            "as_user": " ".join(_init_run_as()) or "(self)",
        }

    async def screenshot(self) -> dict:
        ok, payload = await grab_screenshot()
        if ok:
            return {"ok": True, "image": payload}
        return {"ok": False, "error": payload}

    async def _main(self):
        decky.logger.info(f"DeckMeido 插件已加载，安装目录 {INSTALL_DIR}")
        _init_run_as()

    async def _unload(self):
        decky.logger.info("DeckMeido 插件卸载")

    async def _uninstall(self):
        decky.logger.info("DeckMeido 插件卸载")
