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


def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(INSTALL_DIR), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _append_log(path: Path, text: str) -> None:
    try:
        with open(path, "ab") as f:
            f.write(text.encode())
    except OSError:
        pass


def apply(restart_service: bool = True, tag: str = "") -> dict:
    """切换到指定版本并（可选）重启服务；返回给调用方的说明文字

    tag 为空时按 latest release 的 tag 切换。以前这里是 `git pull --ff-only`，
    要求安装目录是干净仓库：用户只要在安装目录里改过一个字（哪怕只是调试），
    pull 就直接失败，界面只剩一句「升级失败」，还得自己去终端处理。
    现在改成「stash 本地改动 → fetch --tags → checkout <tag>」，
    失败一律回滚到原 commit，并把回滚命令写进日志。
    """
    if not (INSTALL_DIR / ".git").exists():
        raise RuntimeError("当前不是 git 安装，无法自动升级。请重新运行 install.sh")

    log_path = INSTALL_DIR / "update.log"
    target = (tag or "").lstrip("v")
    if not target:
        # 没传版本就查一次最新 release
        target = (latest_release().get("tag") or "").lstrip("v")
    if not target:
        raise RuntimeError("没有可用的版本号：GitHub 上还没有发布任何 release")

    before = _git("rev-parse", "HEAD", timeout=10)
    prev = (before.stdout or "").strip()
    if before.returncode != 0:
        raise RuntimeError(f"读取当前版本失败：{(before.stderr or '').strip()[:300]}")
    _append_log(log_path, f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"升级：{current_version()} → {target}（原 {prev[:8]}）====\n")

    # 有本地改动先 stash：直接 checkout 会报「local changes would be overwritten」
    dirty = _git("status", "--porcelain", timeout=30).stdout.strip()
    stashed = False
    if dirty:
        st = _git("stash", "push", "--include-untracked", "-m",
                  f"deckmeido-autoupdate-{time.strftime('%Y%m%d%H%M%S')}")
        if st.returncode != 0:
            raise RuntimeError(f"暂存本地改动失败：{(st.stderr or '').strip()[:300]}")
        stashed = True
        _append_log(log_path, f"已暂存本地改动（git stash pop 可恢复）\n")

    def _rollback(reason: str) -> None:
        _git("checkout", prev, timeout=60)
        msg = (f"{reason}\n已回滚到升级前的版本（{prev[:8]}）。"
               f"手动回滚命令：git -C {INSTALL_DIR} checkout {prev}")
        if stashed:
            pop = _git("stash", "pop", timeout=60)
            if pop.returncode == 0:
                msg += "\n本地改动已恢复。"
            else:
                msg += "\n本地改动仍保存在 git stash 里（git stash pop 手动恢复）。"
        _append_log(log_path, msg + "\n")
        raise RuntimeError(msg)

    fetch = _git("fetch", "--tags", "--force", "origin", timeout=180)
    if fetch.returncode != 0:
        _rollback(f"git fetch 失败：{(fetch.stderr or fetch.stdout).strip()[:300]}")

    # 优先按 tag 切换；tag 不存在时退回对应的 origin/main
    co = _git("checkout", "--force", f"v{target}", timeout=120)
    if co.returncode != 0:
        co = _git("checkout", "--force", f"{target}", timeout=120)
    if co.returncode != 0:
        _rollback(f"切换到 {target} 失败：{(co.stderr or co.stdout).strip()[:300]}")

    after = (_git("rev-parse", "--short", "HEAD", timeout=10).stdout or "").strip()
    _append_log(log_path, f"已切换到 {target}（{after}）\n")

    if not restart_service:
        note = f"代码已更新到 {target}，重启程序后生效"
        return {"ok": True, "restarted": False, "version": target, "message": note}

    # 日志已在上面的 _append_log 里落过盘，这里只负责拉起升级流程
    with open(log_path, "ab") as log:
        subprocess.Popen(
            ["bash", str(INSTALL_DIR / "install.sh"), "upgrade"],
            cwd=str(INSTALL_DIR), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    return {"ok": True, "restarted": True, "version": target, "log": str(log_path),
            "message": f"已更新到 {target}，服务重启后生效"}


async def verify(expected: str, timeout: float = 60.0) -> dict:
    """升级后自检：等 /api/version 报出新版本才算成功

    升级流程是异步拉起的（install.sh 会先停客户端再重启），调用方拿不到
    它的退出码，只能轮询自己：新进程起来之后报的版本对得上就说明成功。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:18765/api/version", headers={"User-Agent": "DeckMeido"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            got = str(data.get("version") or "").lstrip("v")
            if not expected or got == expected.lstrip("v"):
                return {"ok": True, "version": got}
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            pass
        await asyncio.sleep(1.5)
    return {"ok": False, "version": current_version(), "expected": expected.lstrip("v")}
