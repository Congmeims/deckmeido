"""DeckMeido 原生客户端（PySide6 + QWebEngineView）

内嵌与网页端同一个界面（http://127.0.0.1:18765），两端功能界面永远一致：
以后每次更新只改 server/static/index.html 一处。
启动时自动检测后端核心，未运行则拉起。
URL 带 native=1 标记，网页会据此把「点开图片」改为调用系统默认工具。
窗口最小化后驻留系统托盘，点托盘图标可还原，右键菜单可退出。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QUrl
from PySide6.QtGui import QAction, QIcon
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


def load_token() -> str:
    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))["server"]["token"]
    except Exception:
        return ""


def ensure_backend() -> bool:
    """后端未运行则拉起，返回是否可用"""
    try:
        urllib.request.urlopen(f"{API}/", timeout=2)
        return True
    except Exception:
        pass
    subprocess.Popen(
        [str(DECKMEIDO_DIR / ".venv/bin/python"), "-m", "uvicorn",
         "server.main:app", "--host", "0.0.0.0", "--port", PORT],
        cwd=DECKMEIDO_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{API}/", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    try:
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        app = QApplication(sys.argv)
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

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("DeckMeido")
    # 关掉窗口时先最小化到托盘，由托盘菜单决定是否真退出
    app.setQuitOnLastWindowClosed(False)

    if not ensure_backend():
        QMessageBox.critical(None, "DeckMeido", f"后端核心启动失败，请检查 {DECKMEIDO_DIR} 是否完整（可重跑 install.sh）。")
        return 1

    view = QWebEngineView()
    view.setPage(WebPage(view))
    view.setWindowTitle("DeckMeido")
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

    def on_minimized(state) -> None:
        # Steam Deck 桌面状态栏空间有限，最小化就直接收进托盘
        if state & Qt.WindowMinimized and not view.isMaximized():
            view.hide()
            tray.showMessage("DeckMeido", "已最小化到托盘，点击托盘图标可恢复。", icon, 2000)

    view.installEventFilter(_MinimizeWatcher(on_minimized))

    def on_quit() -> None:
        tray.hide()

    app.aboutToQuit.connect(on_quit)

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
