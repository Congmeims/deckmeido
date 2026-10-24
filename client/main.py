"""DeckMate 原生客户端（PySide6 + QWebEngineView）

内嵌与网页端同一个界面（http://127.0.0.1:18765），两端功能界面永远一致：
以后每次更新只改 server/static/index.html 一处。
启动时自动检测后端核心，未运行则拉起。
URL 带 native=1 标记，网页会据此把「点开图片」改为调用系统默认工具。
"""
from __future__ import annotations

import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

DECKMATE_DIR = Path.home() / "deckmate"
CONFIG_PATH = DECKMATE_DIR / "server" / "config.toml"
API = "http://127.0.0.1:18765"

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
        [str(DECKMATE_DIR / ".venv/bin/python"), "-m", "uvicorn",
         "server.main:app", "--host", "0.0.0.0", "--port", "18765"],
        cwd=DECKMATE_DIR,
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
            None, "DeckMate",
            "缺少 PySide6-WebEngine 组件，请在终端运行：\n"
            "~/deckmate/.venv/bin/pip install PySide6-WebEngine",
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
    app.setApplicationName("DeckMate")

    if not ensure_backend():
        QMessageBox.critical(None, "DeckMate", "后端核心启动失败，请检查 ~/deckmate 是否完整。")
        return 1

    view = QWebEngineView()
    view.setPage(WebPage(view))
    view.setWindowTitle("DeckMate")
    view.resize(WINDOW_W, WINDOW_H)
    icon_path = DECKMATE_DIR / "assets" / "logo.png"
    if icon_path.exists():
        view.setWindowIcon(QIcon(str(icon_path)))
    view.load(QUrl(f"{API}/?token={load_token()}&native=1"))
    view.show()
    # 只放大这个网页，不影响系统里其他 Qt 程序；网页端（浏览器打开）保持原样
    view.setZoomFactor(2.0)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
