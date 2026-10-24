"""DeckMeido 自升级：查 GitHub 最新版本、拉取代码并重启服务

网页设置页的「检查更新 / 立即升级」和 AI 的 self_update 工具共用这里的逻辑。

升级过程会把正在运行的服务换掉，所以分两种走法：
- restart_service=True（用户点了升级、或 systemd 托管）：交给 install.sh upgrade，
  它会同步代码、装依赖、重启服务，等于在终端手动跑一遍
- restart_service=False（进程是被客户端临时拉起的）：只同步文件，不动进程，
  界面会自动重连，新代码下次启动生效
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
REPO = "Congmeims/deckmeido"


def current_version() -> str:
    """当前版本：优先读安装目录的 VERSION（install.sh 生成），退回 git 描述"""
    try:
        ver = (INSTALL_DIR / "VERSION").read_text(encoding="utf-8").strip()
        if ver:
            return ver.lstrip("v")
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["git", "-C", str(INSTALL_DIR), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if out:
            return out.lstrip("v")
    except (OSError, subprocess.SubprocessError):
        pass
    return "0.0.0"


def ver_tuple(ver: str) -> tuple:
    """"0.1.0" → (0, 1, 0)；非数字段落按 0 处理，方便直接比大小"""
    parts = re.findall(r"\d+", ver or "")
    return tuple(int(p) for p in parts[:4]) or (0,)


def latest_release() -> dict:
    """查 GitHub 最新 release（同步 HTTP，调用方放线程里跑）"""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "DeckMeido"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 还没有发布过 release，就不算有更新
            return {"tag": "", "name": "", "notes": "", "url": "", "published_at": ""}
        raise RuntimeError(f"查询更新失败（HTTP {exc.code}）")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"连不上 GitHub：{exc}")
    return {
        "tag": (data.get("tag_name") or "").lstrip("v"),
        "name": data.get("name") or "",
        "notes": (data.get("body") or "")[:4000],
        "url": data.get("html_url") or "",
        "published_at": data.get("published_at") or "",
    }


async def check() -> dict:
    """比对当前版本与 GitHub 最新 release"""
    latest = await asyncio.to_thread(latest_release)
    current = current_version()
    return {
        "current": current,
        "has_update": bool(latest["tag"]) and ver_tuple(latest["tag"]) > ver_tuple(current),
        **latest,
    }


def apply(restart_service: bool = True) -> dict:
    """拉取最新代码并（可选）重启服务；返回给调用方的说明文字"""
    if not (INSTALL_DIR / ".git").exists():
        raise RuntimeError("当前不是 git 安装，无法自动升级。请重新运行 install.sh")

    pull = subprocess.run(
        ["git", "-C", str(INSTALL_DIR), "pull", "--ff-only"],
        capture_output=True, text=True, timeout=120,
    )
    if pull.returncode != 0:
        raise RuntimeError(f"git pull 失败：{(pull.stderr or pull.stdout).strip()[:400]}")

    if not restart_service:
        note = "代码已更新，重启程序后生效"
        return {"ok": True, "restarted": False, "message": note}

    # 日志留在安装目录，升级后服务重启也能查
    log_path = INSTALL_DIR / "update.log"
    log = open(log_path, "ab")
    log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 升级到最新版本 ====\n".encode())
    subprocess.Popen(
        ["bash", str(INSTALL_DIR / "install.sh"), "upgrade"],
        cwd=str(INSTALL_DIR), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    return {"ok": True, "restarted": True, "log": str(log_path),
            "message": "升级已开始，服务重启后会自动回到新版"}
