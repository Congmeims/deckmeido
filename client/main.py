"""DeckMeido 原生客户端（PySide6 + QWebEngineView）

内嵌与网页端同一个界面（http://127.0.0.1:18765），两端功能界面永远一致：
以后每次更新只改 server/static/index.html 一处。
启动时自动检测后端核心，未运行则拉起。
URL 带 native=1 标记，网页会据此把「点开图片」改为调用系统默认工具。
窗口最小化后驻留系统托盘，点托盘图标可还原，右键菜单可退出。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
import hashlib
from pathlib import Path

from PySide6.QtCore import QEvent, QLibraryInfo, QObject, Qt, QTimer, QTranslator, QUrl
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

# 安装目录：默认取本文件的两级上层（client/main.py → 仓库根），
# 这样装到任意路径都能用；DECKMEIDO_DIR 可显式覆盖
DECKMEIDO_DIR = Path(
    os.environ.get("DECKMEIDO_DIR") or Path(__file__).resolve().parent.parent
)
CONFIG_PATH = DECKMEIDO_DIR / "server" / "config.toml"
PORT = os.environ.get("DECKMEIDO_PORT", "18765")
API = f"http://127.0.0.1:{PORT}"

# Steam Deck 桌面 1280x800，窗口不超过它
WINDOW_W, WINDOW_H = 1240, 760


BACKEND_LOG = DECKMEIDO_DIR / "server" / "backend.log"
_BACKEND_LOG_MAX = 5 * 1024 * 1024   # 5 MB，只留一代 backend.log.1
_log_handle = None

# 客户端崩溃日志：切游戏模式时 gamescope 会话重建会掐断 Wayland 连接，
# Qt 在那种情况下是 abort()，Python 层 try/except 拦不住，只有 faulthandler
# 能在进程死前把栈写下来。没有它就只能看到一句 "Wayland connection broke"。
CRASH_LOG = DECKMEIDO_DIR / "server" / "client-crash.log"


def _enable_crash_log() -> None:
    """把 C 层致命信号（SIGSEGV / SIGABRT 等）的栈写到文件里"""
    try:
        import faulthandler

        f = open(CRASH_LOG, "a", encoding="utf-8")
        faulthandler.enable(file=f, all_threads=True)
        f.write(f"\n===== client start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        f.flush()
        globals()["_crash_handle"] = f  # 保持引用，别让 GC 把文件关掉
    except Exception:
        pass



def load_token() -> str:
    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))["server"]["token"]
    except Exception:
        return ""


def _backend_log_fd():
    """后端 stdout/stderr 的去处：写文件，而不是 /dev/null

    以前一律 DEVNULL，后端一崩连一行 traceback 都留不下——12:52 那次
    崩溃到现在都查不出死因，就是因为证据被自己丢掉了。
    句柄在父进程里持有到退出为止，避免每次 Popen 漏一个 fd。
    """
    global _log_handle
    if _log_handle is not None:
        return _log_handle.fileno()
    try:
        BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
        if BACKEND_LOG.exists() and BACKEND_LOG.stat().st_size > _BACKEND_LOG_MAX:
            os.replace(BACKEND_LOG, BACKEND_LOG.with_name("backend.log.1"))
        _log_handle = open(BACKEND_LOG, "ab", buffering=0)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _log_handle.write(f"\n===== backend start {stamp} =====\n".encode())
        return _log_handle.fileno()
    except Exception:
        _log_handle = None
        return subprocess.DEVNULL


def backend_alive(timeout: float = 10.0) -> bool:
    """后端是否在应答（带 token 问 /api/status，确认是「我们的」后端）

    超时从 2 秒放宽到 10 秒：后端忙着流式生成、跑 to_thread 里的命令时，
    2 秒内没回话是常态，旧值会把「忙」误判成「死」，于是又拉起一份。
    """
    token = load_token()
    url = f"{API}/api/status"
    if token:
        url += f"?token={token}"
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def ensure_backend() -> bool:
    """后端未运行则拉起，返回是否可用

    这是整套服务唯一的起点：桌面快捷方式 → 客户端 → 后端 → 遮罩。
    后端自己也带单实例守卫（server/singleton.py），即便被别处重复拉起，
    第二个也会立刻退出，不会出现两份后端抢端口、各存一份历史的情况。
    """
    try:
        urllib.request.urlopen(f"{API}/", timeout=2)
        return True
    except Exception:
        pass
    print(f"[DeckMeido] 后端未运行，正在拉起，日志：{BACKEND_LOG}", file=sys.stderr)
    kicker = _start_backend()
    # 记下句柄交给调用方，watchdog 才认得出「这是我刚拉起来的那个」
    global _STARTED_BACKEND
    _STARTED_BACKEND = kicker
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{API}/", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


_STARTED_BACKEND = None


def _start_backend():
    """真正执行 subprocess.Popen 的那一步，单独抽出来让 ensure_backend 能记句柄"""
    python = DECKMEIDO_DIR / ".venv/bin/python"
    if not python.exists():
        print(f"[DeckMeido] 找不到解释器 {python}，请重跑 install.sh", file=sys.stderr)
        return None
    try:
        return subprocess.Popen(
            [str(python), "-m", "uvicorn",
             "server.main:app", "--host", "0.0.0.0", "--port", PORT],
            cwd=DECKMEIDO_DIR,
            stdout=_backend_log_fd(), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:
        # 以前直接 return None，用户只能看到「等了 15 秒还没起来」，
        # 真正的原因（权限/缺文件）全丢了
        print(f"[DeckMeido] 拉起后端失败：{exc}", file=sys.stderr)
        return None


def _listener_on_port(port: str) -> int | None:
    """查出正在监听该端口的进程 pid（查不到返回 None）

    退出时用：后端是自己 spawn 的、又放在新会话里，即便这里没记句柄，
    也能靠端口找到它，保证「关掉客户端就把服务一起收走」。
    """
    try:
        out = subprocess.run(
            ["ss", "-ltnpH", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    # 例：users:(("python",pid=23248,fd=6))
    for chunk in out.split("pid="):
        digits = chunk.split(",")[0].strip()
        if digits.isdigit():
            return int(digits)
    return None


def checkpoint_backend() -> str:
    """退出前请后端把未落盘的内容刷下去（正在生成的半截回答、内存历史）

    返回状态字符串，仅用于日志：ok / skipped / failed:xxx。
    这一步失败不能挡住退出流程，所以异常一律吞掉。
    """
    token = load_token()
    url = f"{API}/api/checkpoint"
    if token:
        url += f"?token={token}"
    try:
        with urllib.request.urlopen(url, data=b"", timeout=4) as r:
            body = r.read().decode("utf-8", "replace")
        return f"ok {body[:120]}"
    except Exception as e:
        return f"failed:{e}"


def stop_backend() -> None:
    """收掉本机后端（含它拉起的遮罩），别让服务在客户端退出后继续裸奔

    只收「自己拉起来的那个」：启动时后端已经在跑（用户手动起的、或上次
    客户端被 kill 留下的），就不归本次退出管——按端口一律 SIGTERM 会把
    别人正在用的后端连同那一轮回答一起干掉。
    """
    proc = _STARTED_BACKEND
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    # 先请它把正在生成的回答与内存历史落盘，再让它退出。
    # 以前直接 SIGTERM：那一轮回答还在流式生成中，进程一死就整段没了。
    print(f"[DeckMeido] 退出前 checkpoint：{checkpoint_backend()}", file=sys.stderr)
    try:
        os.kill(pid, 15)  # SIGTERM：uvicorn 会走 shutdown 钩子收掉遮罩
    except ProcessLookupError:
        return
    except Exception:
        return
    for _ in range(80):  # 最多等 8 秒（shutdown 钩子里还要落一次盘，别太早 SIGKILL）
        if proc.poll() is not None or _listener_on_port(PORT) is None:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except Exception:
        pass


def acquire_single_instance() -> QLocalServer | None:
    """单实例锁：全局只允许一个客户端在跑。

    已经有实例在运行时，把「唤起主窗口」的请求发给它，本次直接退出
    （返回 None）；本次是第一个实例则返回持有本地套接字的 QLocalServer
    （调用方必须保持引用，否则会被回收）。

    用 QLocalServer 而不是锁文件，好处是进程崩溃后系统自动释放，
    不会留下死锁。
    """
    # 名字带上安装目录指纹：不同安装位置可以各跑一个，互不打架
    name = "DeckMeido-" + hashlib.sha1(
        str(DECKMEIDO_DIR).encode("utf-8")
    ).hexdigest()[:12]

    def poke_existing() -> bool:
        """连上已有实例并让它显示窗口，成功返回 True"""
        sock = QLocalSocket()
        sock.connectToServer(name)
        if not sock.waitForConnected(300):
            return False
        sock.write(b"show")
        # 只 flush：disconnectFromServer 会等挂起数据写完，
        # 而对端可能收完就断开，此时再调 waitForBytesWritten 会被 Qt 拒并打印
        # "not allowed in UnconnectedState" 警告（try/except 拦不住）
        sock.flush()
        sock.disconnectFromServer()
        return True

    if poke_existing():
        return None

    # 连不上：要么真没有实例，要么上次崩溃留下了残留的套接字文件，清掉再监听
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        # 极小概率和另一个正在启动的实例撞车：它抢先了就让位给它
        if poke_existing():
            return None
        # 实在监听不了（权限/路径异常）：宁可放弃单实例保护，也让客户端能起来
        print(f"[DeckMeido] 单实例监听失败：{server.errorString()}", file=sys.stderr)
    return server


def install_translators(app: QApplication) -> None:
    """给 Qt 内置界面（右键菜单、对话框等）装上中文翻译

    QWebEngineView 的右键菜单（Reload / Save page / Back…）是 Qt/Chromium
    自带的，不走网页内容，所以改网页没用——必须在 QApplication 上装
    QTranslator 才能变成中文。翻译 .qm 随 PySide6 一起装着，只是没人加载。
    """
    # 允许用 DECKMEIDO_QT_LANG 覆盖，默认跟随系统；系统若为英文则退回 zh_CN
    lang = os.environ.get("DECKMEIDO_QT_LANG")
    if not lang:
        for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
            val = os.environ.get(var, "")
            if val.lower().startswith("zh"):
                lang = "zh_CN" if "cn" in val.lower() or "hans" in val.lower() else "zh_TW"
                break
    if not lang:
        lang = "zh_CN"

    base = os.environ.get("DECKMEIDO_QT_TRANSLATIONS") or QLibraryInfo.path(
        QLibraryInfo.LibraryPath.TranslationsPath
    )
    # Qt 本体 + WebEngine（右键菜单在 WebEngine 里）+ 基础控件，三个都要装
    for stem in ("qtbase", "qtwebengine", "qt", "qtmultimedia", "qtwebsockets"):
        path = os.path.join(base, f"{stem}_{lang}.qm")
        if not os.path.exists(path):
            continue
        tr = QTranslator(app)
        if tr.load(path):
            app.installTranslator(tr)


def main() -> int:
    _enable_crash_log()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("DeckMeido")
    install_translators(app)
    # 关掉窗口时先最小化到托盘，由托盘菜单决定是否真退出
    app.setQuitOnLastWindowClosed(False)

    # 被 kill / 注销 / systemd 停止时也要走正常退出流程，
    # 否则 aboutToQuit 不触发，本机的后端和遮罩就成了没人管的孤儿。
    # 注意：Qt 事件循环里不能直接干重活，转成 queued 调用再退。
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            # 信号处理函数在字节码间隙执行，直接调 Qt 有风险，
            # 转成一次 queued 调用交给事件循环
            signal.signal(sig, lambda *_: QTimer.singleShot(0, app.quit))
        except Exception:
            pass

    # ---- 单实例：先查重再加载 WebEngine，重复点击瞬间返回，不浪费内存 ----
    single = acquire_single_instance()
    if single is None:
        return 0

    try:
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        QMessageBox.critical(
            None, "DeckMeido",
            "缺少 PySide6-WebEngine 组件，请在终端运行：\n"
            f"{DECKMEIDO_DIR}/.venv/bin/pip install PySide6-WebEngine",
        )
        return 1

    class WebPage(QWebEnginePage):
        """自动授予麦克风权限，让语音输入在客户端里可用"""

        def permissionRequested(self, permission):
            if permission.feature() in (
                QWebEnginePage.Feature.MediaAudioCapture,
                QWebEnginePage.Feature.MediaAudioVideoCapture,
                QWebEnginePage.Feature.ClipboardReadWrite,
            ):
                permission.grant()
            else:
                permission.deny()

    if not ensure_backend():
        QMessageBox.critical(None, "DeckMeido", f"后端核心启动失败，请检查 {DECKMEIDO_DIR} 是否完整（可重跑 install.sh）。")
        return 1

    view = QWebEngineView()
    view.setPage(WebPage(view))
    view.setWindowTitle("DeckMeido")
    # 屏蔽网页区右键菜单（Reload/Save page 等 Qt 自带项，在本客户端无用）
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    view.resize(WINDOW_W, WINDOW_H)
    icon_path = DECKMEIDO_DIR / "assets" / "logo.png"
    icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
    view.setWindowIcon(icon)
    view.load(QUrl(f"{API}/?token={load_token()}&native=1"))
    view.show()
    # 只放大这个网页，不影响系统里其他 Qt 程序；网页端（浏览器打开）保持原样
    view.setZoomFactor(1.2)

    # ---- 系统托盘：最小化后仍可在托盘找回 ----
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("DeckMeido")

    def restore() -> None:
        if view.isFullScreen():
            # 全屏时 showNormal() 会顺带把窗口还原成小窗，相当于「恢复」把用户的
            # 全屏给取消了；全屏状态下只提窗，不动状态
            view.showFullScreen()
        else:
            view.showNormal()
        view.raise_()
        view.activateWindow()

    menu = QMenu()
    act_show = QAction("显示主窗口", menu)
    act_show.triggered.connect(restore)
    act_quit = QAction("退出", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_show)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: restore() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )
    tray.show()

    # ---- 重复启动时唤起已有窗口 ----
    # 用户再点一次图标 → 新进程连上这里的本地套接字就退出，
    # 这个已有实例收到连接后把窗口提到前台，而不是又开一个托盘图标。
    def on_second_instance() -> None:
        while single.hasPendingConnections():
            conn = single.nextPendingConnection()
            if conn is None:
                break
            conn.disconnected.connect(conn.deleteLater)
            restore()
            conn.disconnectFromServer()

    single.newConnection.connect(on_second_instance)

    backend = {"proc": _STARTED_BACKEND}

    def kick_backend() -> None:
        global _STARTED_BACKEND
        backend["proc"] = _start_backend()
        # watchdog 重拉的后端同样是我们 spawn 的，退出时要一并收走
        _STARTED_BACKEND = backend["proc"]

    watch_state = {"last_kick": 0.0, "misses": 0}

    def watchdog() -> None:
        """后端挂了就自动重拉。

        以前这个职责在 systemd（Restart=on-failure）；现在服务只由客户端拉起，
        客户端就得自己看着——否则后端崩一次，界面就一直连不上。

        这里**不发 HTTP**：watchdog 跑在 Qt 主线程上，探活一旦超时就会把界面
        冻住（旧版 2 秒硬超时，后端忙着流式生成时既卡界面又容易误判成「已死」）。
        改成看进程句柄 + 端口监听，都是毫秒级返回。
        """
        proc = backend["proc"]
        if proc is not None:
            if proc.poll() is None:
                watch_state["misses"] = 0
                return  # 句柄在、进程活
        elif _listener_on_port(PORT) is not None:
            watch_state["misses"] = 0
            return  # 没句柄（启动时连的是既有后端），但端口有人在听
        # 到这里只能说明「句柄没了/端口没人听」，还不能断定后端真死：
        # `ss` 偶尔会慢一拍或查不到，直接 kick 就会在回答生成到一半时
        # 杀掉后端（用户视角：回复又丢了）。要求连续两次探活失败才动手。
        watch_state["misses"] = watch_state.get("misses", 0) + 1
        if watch_state["misses"] < 2:
            return
        # 60 秒冷却，避免反复拉起失败时刷屏、刷通知
        now = time.monotonic()
        if now - watch_state["last_kick"] < 60:
            return
        watch_state["last_kick"] = now
        watch_state["misses"] = 0
        kick_backend()
        if backend["proc"] is None:
            # 拉起失败别再 restore 打扰用户：托盘提示一下，下一轮再试
            tray.showMessage("DeckMeido", "后端重启失败，详见 server/backend.log", icon, 3000)
            return
        # 不 restore()：用户可能正在别的窗口里干活，硬把本窗口提到前台很烦，
        # 托盘给个提示就够，需要时点托盘图标回来
        tray.showMessage("DeckMeido", "后端已自动重启。", icon, 2500)

    timer = QTimer()
    timer.setInterval(5000)
    timer.timeout.connect(watchdog)
    timer.start()

    def on_minimized(state) -> None:
        # 全屏 ↔ 普通切换也会走到这里，那不算「最小化」，别把窗口藏进托盘
        if state & Qt.WindowFullScreen:
            return
        # Steam Deck 桌面状态栏空间有限，最小化就直接收进托盘
        if state & Qt.WindowMinimized and not view.isMaximized():
            view.hide()
            tray.showMessage("DeckMeido", "已最小化到托盘，点击托盘图标可恢复。", icon, 2000)

    # 事件过滤器对象必须留引用：installEventFilter 不持有所有权（Qt 只存裸指针），
    # 这里如果写成 installEventFilter(_MinimizeWatcher(...))，Watcher 会被 GC，
    # 窗口状态一变就访问已释放对象
    watcher = _MinimizeWatcher(on_minimized)
    watcher.setParent(app)
    view.installEventFilter(watcher)

    def on_quit() -> None:
        tray.hide()
        # 客户端是唯一入口，退出时把自己拉起的后端一并收走（后端会再收掉遮罩），
        # 否则下次启动就成了「旧后端还在跑、新客户端连过去」的混乱状态
        stop_backend()

    app.aboutToQuit.connect(on_quit)

    # 信号处理（转 queued 调用再 app.quit）已在 main() 开头统一注册，这里不要再注册一遍：
    # 在事件循环里直接调 app.quit() 有重入风险，且会覆盖掉前面那份正确的处理函数。

    return app.exec()


def _MinimizeWatcher(callback):
    """把「窗口最小化」事件转给回调（避免额外继承一份 QWebEngineView）"""

    class Watcher(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.WindowStateChange:
                callback(obj.windowState())
            return False

    return Watcher()


if __name__ == "__main__":
    sys.exit(main())
