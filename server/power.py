"""桌面模式下阻止息屏 / 休眠

需求：处在**桌面模式**、并且 DeckMeido 正在执行命令或显示操作遮罩时，
屏幕不能黑、机器不能睡。游戏模式（gamescope 在跑）完全不管——那时候
屏幕上本来就不是我们，别去干扰 Steam 自己的电源策略。

实现上是两个「抑制」叠在一起，缺一个都会漏：

1. `systemd-inhibit --what=idle:sleep --mode=block` 起一个 `sleep infinity`
   子进程常驻，抑制随进程存活——进程一死（包括自己被 kill、崩溃）抑制
   自动释放，不会像 D-Bus 租约那样留下永远不放的 cookie。
2. KDE 的 `org.freedesktop.ScreenSaver.Inhibit`。logind 的 idle 抑制只挡
   「按空闲挂起」，Plasma 的息屏/变暗是 PowerDevil 自己按 KIdleTime 算的，
   它认的是 ScreenSaver 的抑制 cookie（视频播放器就是这么防黑屏的）。
   只做第 1 条时屏幕照样会黑，必须补这一条。

两条都是同步外部命令，调用点却在事件循环里（工具执行前后、遮罩开关），
所以 hold/release 只登记原因，真正的挂/收由后台线程做，不阻塞对话。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

WHO = "DeckMeido"
# 持有抑制期间核对间隔：抑制进程被外部杀掉、模式从桌面切到游戏，最多这么久被发现。
# 没有抑制时线程不轮询，只在 hold/release 时被唤醒（见 AwakeKeeper._loop），
# 空闲状态下一个子进程都不会起。
POLL_INTERVAL = 2.0
# 模式判定缓存：切模式后最多这么久才反映过来，避免频繁跑 pw-dump
MODE_TTL = 3.0


def _log(msg: str) -> None:
    print(f"[DeckMeido] {msg}", file=sys.stderr)


def _session_env() -> dict:
    """桌面会话环境：systemd 用户服务里默认没有 DISPLAY / D-Bus 地址"""
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    env.setdefault("DISPLAY", ":0")
    return env


def desktop_mode() -> bool:
    """当前是否桌面模式。

    判据和 get_status、depcheck 共用 tools.mode_is_gaming()：PipeWire 里有没有
    gamescope 的视频节点。以前这里是 `pgrep -x gamescope`，而 SteamOS 的 gamescope
    带着一长串参数启动、comm 被截断到 15 字节，pgrep 恒匹配不上 —— 于是人在游戏
    模式里也会被当成桌面模式，白白挂上息屏抑制。
    探测失败按桌面模式处理：宁可多抑制一次，也不要因为判断不出来而在用户盯着屏幕时黑屏。
    """
    # 延迟导入：tools 在模块顶层 import 了 power，这里不能反过来在顶层 import 它
    from . import tools
    return not tools.mode_is_gaming()


class AwakeKeeper:
    """按「原因」计数地持有抑制；没有原因时立刻释放"""

    def __init__(self) -> None:
        self._enabled = True
        self._why = "DeckMeido 正在操作设备"
        self._held: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._cookie: int | None = None
        self._mode_at = 0.0
        self._mode = True
        self._thread: threading.Thread | None = None

    # ---------- 对外接口 ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, on: bool) -> None:
        """开关变化时唤醒后台线程：关掉要立刻释放，打开要立刻补挂"""
        self._enabled = bool(on)
        self._wake.set()

    def hold(self, reason: str, why: str = "") -> None:
        """登记一个「别睡」的原因（command / overlay）"""
        with self._lock:
            new = reason not in self._held
            self._held.add(reason)
            if why:
                self._why = why
        self._ensure_thread()
        if new:
            self._wake.set()

    def release(self, reason: str) -> None:
        with self._lock:
            self._held.discard(reason)
        self._wake.set()

    def release_all(self) -> None:
        with self._lock:
            self._held.clear()
        self._wake.set()

    def shutdown(self) -> None:
        """服务退出：停线程并确定释放，别把抑制留给下一个进程"""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)
        self._drop("服务退出")

    def status(self) -> dict:
        with self._lock:
            held = sorted(self._held)
        return {
            "enabled": self.enabled,
            "reasons": held,
            "active": bool(self._proc is not None and self._proc.poll() is None),
            "desktop_mode": self._mode,
        }

    # ---------- 内部 ----------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="awake-keeper", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """只有「要挂抑制」或「抑制还挂着」时才定期核对，空闲时阻塞等事件

        空闲（没有 hold、抑制也已经收干净）时不再定时醒来：这时候没有要挂的抑制，
        也没有可能被外部杀掉的进程，轮询只会白白去跑一次模式探测。
        注意判断条件里不能只看 _held：hold 后立刻 release 的那一轮，线程可能还没
        醒来就看到空集合，得靠「进程还活着」把释放做完。
        """
        while not self._stop.is_set():
            with self._lock:
                wanted = bool(self._held) and self._enabled
            if wanted or self._proc is not None or self._cookie is not None:
                try:
                    self._reconcile()
                except Exception as e:      # 电源抑制失败绝不能拖垮服务
                    _log(f"保持常亮失败：{e}")
                self._wake.wait(timeout=POLL_INTERVAL)
            else:
                self._wake.wait()
            self._wake.clear()

    def _refresh_mode(self) -> bool:
        now = time.monotonic()
        if now - self._mode_at > MODE_TTL:
            try:
                self._mode = desktop_mode()
            except Exception as e:
                # 判不出来按桌面处理：宁可多抑制一次，也别在用户盯着屏幕时黑屏
                _log(f"模式判定失败，按桌面模式处理：{e}")
                self._mode = True
            self._mode_at = now
        return self._mode

    def _reconcile(self) -> None:
        with self._lock:
            wanted = bool(self._held)
        want = wanted and self.enabled and self._refresh_mode()
        alive = self._proc is not None and self._proc.poll() is None
        if want and not alive:
            # 模式刚切到桌面、或者抑制进程被外部杀掉，都要重新挂上
            self._apply(self._why)
        elif not want and (alive or self._cookie is not None):
            self._drop("条件结束" if self.enabled else "功能已关闭")

    def _apply(self, why: str) -> None:
        self._drop("重新申请")
        self._proc = self._start_inhibit(why)
        self._cookie = self._screensaver_inhibit(why)

    def _start_inhibit(self, why: str) -> subprocess.Popen | None:
        if not shutil.which("systemd-inhibit"):
            return None
        try:
            proc = subprocess.Popen(
                ["systemd-inhibit", "--what=idle:sleep", "--mode=block",
                 f"--who={WHO}", f"--why={why}", "sleep", "infinity"],
                env=_session_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # 独立进程组：收的时候一次带走 systemd-inhibit 和它的 sleep 子进程
                start_new_session=True,
            )
        except Exception as e:
            _log(f"systemd-inhibit 启动失败：{e}")
            return None
        # 立刻验证它真的挂上了，而不是启动就退出（权限/参数问题会这样）
        time.sleep(0.1)
        if proc.poll() is not None:
            _log(f"systemd-inhibit 未能生效（退出码 {proc.returncode}），"
                 "电源抑制可能不可用")
            return None
        _log(f"已阻止息屏/休眠：{why}")
        return proc

    def _screensaver_inhibit(self, why: str) -> int | None:
        """KDE 的息屏抑制：拿到 cookie 才算成功（cookie 用于之后解除）"""
        if not shutil.which("gdbus"):
            return None
        try:
            r = subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.ScreenSaver",
                 "--object-path", "/ScreenSaver",
                 "--method", "org.freedesktop.ScreenSaver.Inhibit",
                 WHO, why],
                capture_output=True, text=True, timeout=5, env=_session_env(),
            )
        except Exception:
            return None
        if r.returncode != 0:
            # 非 Plasma 桌面（没有这个接口）属正常，logind 那条已经够用
            return None
        m = re.search(r"uint32\s+(\d+)", r.stdout)
        return int(m.group(1)) if m else None

    def _screensaver_release(self, cookie: int | None) -> None:
        if cookie is None or not shutil.which("gdbus"):
            return
        try:
            subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.ScreenSaver",
                 "--object-path", "/ScreenSaver",
                 "--method", "org.freedesktop.ScreenSaver.UnInhibit",
                 str(cookie)],
                capture_output=True, timeout=5, env=_session_env(),
            )
        except Exception:
            pass

    def _drop(self, why: str) -> None:
        proc, self._proc = self._proc, None
        cookie, self._cookie = self._cookie, None
        if proc is not None:
            try:
                # 杀进程组：systemd-inhibit 的 sleep 子进程要一起走，
                # 否则光杀父进程会留下一个还在占着抑制的 sleep
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=2)
                except Exception:
                    pass
        self._screensaver_release(cookie)
        if proc is not None or cookie is not None:
            _log(f"已恢复息屏/休眠（{why}）")


# 全局单例：tools.py 和 main.py 都用这一份
keeper = AwakeKeeper()
