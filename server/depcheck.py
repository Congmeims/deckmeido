"""控制类控件自检：给「桌面操作」能力的可用性打分。

为什么要实测而不是查 pacman：SteamOS 换镜像（BUILD_ID 变动）时，/usr 下由
pacman 装的包会被整片冲掉，而包数据库记录不一定同步，只查 `pacman -Q` 会出现
「记录在、命令没了」的假象（wtype 就这么消失过一次）。所以一律实测命令能不能用。

同理 ydotoold 必须是 systemd 用户单元托管的常驻进程：裸跑 `ydotoold &` 重启即失效，
所以这里连 enabled / active 一起报出来，网页上能直接看出「装了但没起」。

探测有成本（要起子进程），所以结果带缓存，默认 60 秒内复用；前端每 30 秒拉一次
状态，基本每次都命中缓存。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time

CACHE_TTL = 60.0

# 控件清单：顺序即网页上展示的顺序
#   key      前端用来标识
#   label    中文短名
#   critical 坏了就等于「没法替你在桌面上动手」
CONTROLS = [
    ("ydotool", "点击输入", True),
    ("kdotool", "窗口聚焦", True),
    ("wtype", "原生打字", False),
    ("espeak", "语音播报", False),
    ("fakeroot", "构建依赖", False),
    ("spectacle", "截图工具", False),
]

_lock = threading.Lock()
_cache: dict | None = None
_cache_at = 0.0


def _run(args: list[str], env: dict | None = None, timeout: float = 3.0) -> tuple[int, str]:
    """跑一条命令，返回 (returncode, 输出)；起不来时返回 (127, 错误信息)"""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, "调用超时"
    except OSError as exc:
        return 126, str(exc)


def _ydotool_socket() -> str:
    # ydotool 1.0 默认读 $YDOTOOL_SOCKET，没有就用 /run/user/<uid>/.ydotool_socket
    return os.environ.get("YDOTOOL_SOCKET") or f"/run/user/{os.getuid()}/.ydotool_socket"


def _probe_ydotool() -> tuple[bool, str]:
    if not shutil.which("ydotool"):
        return False, "未安装"
    if not shutil.which("ydotoold"):
        return False, "守护进程缺失，需重装 ydotool"
    path = _ydotool_socket()
    if not os.path.exists(path):
        return False, "ydotoold 未运行，点击输入不可用"
    # 只看进程不够：socket 没建出来照样点不动。连一下确认真的在听。
    # 注意 ydotoold 用的是 SOCK_DGRAM（1.0.x），用 SOCK_STREAM 连会报
    # ENOPROTOOPT（Protocol wrong type for socket），别误判成故障
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        s.connect(path)
    except OSError as exc:
        return False, f"socket 连不上（{exc.strerror or exc}）"
    finally:
        s.close()
    # 这里不再发 mousemove 之类的探针命令：那会把用户的光标真的挪走
    return True, ""


def _probe_kdotool() -> tuple[bool, str]:
    if not shutil.which("kdotool"):
        return False, "未安装"
    # kdotool 需要知道会话是 Plasma 6，非 KDE 会话下会直接报错
    rc, out = _run(["kdotool", "getactivewindow"], env={"KDE_SESSION_VERSION": "6"})
    if rc == 0:
        return True, ""
    return False, f"调用失败（{out.splitlines()[0][:60] if out else '无输出'}）"


def _probe_wtype() -> tuple[bool, str]:
    if not shutil.which("wtype"):
        return False, "未安装（SteamOS 镜像更新后常被冲掉）"
    # 无参数时 wtype 会打印用法并返回 1，这不是故障；返回 126/127 才是真坏
    rc, out = _run(["wtype"], timeout=3.0)
    if rc in (126, 127):
        return False, f"命令无法执行（{out[:60]}）"
    return True, ""


def _probe_fakeroot() -> tuple[bool, str]:
    if not shutil.which("fakeroot"):
        return False, "未安装（编译 AUR 包会失败）"
    rc, out = _run(["fakeroot", "true"])
    if rc == 0:
        return True, ""
    return False, f"调用失败（{out[:60] or rc}）"


def _probe_espeak() -> tuple[bool, str]:
    # espeak-ng 提供的命令名是 espeak-ng，也有旧名 espeak 的软链
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        return False, "未安装（语音播报不可用）"
    # --version 正常返回 0；返回 126/127 说明文件在但执行不了（镜像更新常见）
    rc, out = _run([exe, "--version"], timeout=3.0)
    if rc in (126, 127):
        return False, f"命令无法执行（{out[:60]}）"
    return True, ""


def _probe_spectacle() -> tuple[bool, str]:
    if not shutil.which("spectacle"):
        return False, "未安装（KDE 一般自带）"
    # 不真的截图（会弹窗口）：只确认二进制可以执行
    rc, out = _run(["spectacle", "--help"], timeout=5.0)
    if rc in (126, 127):
        return False, f"命令无法执行（{out[:60]}）"
    return True, ""


PROBES = {
    "ydotool": _probe_ydotool,
    "kdotool": _probe_kdotool,
    "wtype": _probe_wtype,
    "espeak": _probe_espeak,
    "fakeroot": _probe_fakeroot,
    "spectacle": _probe_spectacle,
}


def _systemd_state() -> dict:
    """ydotoold 的托管情况：enabled 决定重启后还在不在，active 决定现在能不能用"""
    def query(*args: str) -> str:
        rc, out = _run(["systemctl", "--user", *args], timeout=3.0)
        return out if rc == 0 else ""

    return {
        "enabled": query("is-enabled", "ydotool.service") == "enabled",
        "active": query("is-active", "ydotool.service") == "active",
    }


def _collect() -> dict:
    items = []
    for key, label, critical in CONTROLS:
        try:
            ok, detail = PROBES[key]()
        except Exception as exc:                      # 探测本身出错也不能拖垮状态接口
            ok, detail = False, f"探测异常：{exc}"
        items.append({
            "key": key, "label": label, "ok": ok,
            "critical": critical, "detail": detail,
        })

    ok_count = sum(1 for i in items if i["ok"])
    total = len(items)
    # 灯色：全绿 / 有坏但还能干活（黄） / 关键控件坏了（红）
    if ok_count == total:
        level = "ok"
    elif any(not i["ok"] and i["critical"] for i in items):
        level = "danger"
    else:
        level = "warn"

    return {
        "items": items,
        "ok": ok_count,
        "total": total,
        "level": level,
        "ydotoold": _systemd_state(),
        # 一键修复要 sudo 装系统包，需要用户在终端里敲密码；
        # 能不能免密执行决定网页上那个按钮是直接干活还是先教主人怎么走
        "sudo_nopasswd": sudo_nopasswd(),
        "checked_at": int(time.time()),
    }


def sudo_nopasswd() -> bool:
    """deck 是否能免密 sudo（装了 wheel 的 NOPASSWD 才有）"""
    if not shutil.which("sudo"):
        return False
    rc, _ = _run(["sudo", "-n", "true"], timeout=3.0)
    return rc == 0


def fix_ydotoold() -> tuple[bool, str]:
    """只做不需要 root 的那一半：把 ydotoold 交给 systemd 用户单元托管。

    这是「装了但没起」最常见的形态：裸跑 `ydotoold &` 重启即失效，
    或者镜像更新后 socket 没重建。enable --now 一步解决且能持久。
    """
    if not shutil.which("ydotool"):
        return False, "ydotool 未安装，需要先装包（要 sudo）"
    if not shutil.which("systemctl"):
        return False, "没有 systemctl，无法托管守护进程"

    def run(*args: str) -> tuple[int, str]:
        return _run(["systemctl", "--user", *args], timeout=15.0)

    run("daemon-reload")
    # 裸跑进程会占着 socket，systemd 起不来，先清掉
    rc, out = run("is-active", "ydotool.service")
    if out != "active":
        _run(["pkill", "-x", "ydotoold"], timeout=5.0)
        try:
            os.unlink(_ydotool_socket())
        except OSError:
            pass
    rc, out = run("enable", "--now", "ydotool.service")
    if rc != 0:
        return False, f"启动失败：{out.splitlines()[-1][:80] if out else '无输出'}"
    # 等 socket 建出来（守护进程初始化要几十毫秒）
    for _ in range(20):
        if os.path.exists(_ydotool_socket()):
            break
        time.sleep(0.05)
    ok, detail = _probe_ydotool()
    invalidate()
    if ok:
        return True, "ydotoold 已由 systemd 托管并开机自启"
    return False, detail or "服务已启动但 socket 仍不可用"


def summary(force: bool = False) -> dict:
    """返回控件自检结果；force=True 时跳过缓存（自检/修复后立即刷新用）"""
    global _cache, _cache_at
    with _lock:
        fresh = _cache is not None and (time.monotonic() - _cache_at) < CACHE_TTL
        if fresh and not force:
            return _cache
    data = _collect()
    with _lock:
        _cache, _cache_at = data, time.monotonic()
    return data


def invalidate() -> None:
    """装完/修完控件后调用，让下一次状态查询重新实测"""
    global _cache, _cache_at
    with _lock:
        _cache, _cache_at = None, 0.0
