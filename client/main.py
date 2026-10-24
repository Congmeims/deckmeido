"""DeckMate 原生客户端（PySide6）

启动时自动检测后端核心（localhost:18765），未运行则拉起。
配置直接读 ~/deckmate/server/config.toml，无需输入 token。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QCheckBox, QTextBrowser,
    QVBoxLayout, QWidget,
)

DECKMATE_DIR = Path.home() / "deckmate"
CONFIG_PATH = DECKMATE_DIR / "server" / "config.toml"
API = "http://127.0.0.1:18765"


def load_token() -> str:
    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))["server"]["token"]
    except Exception:
        return ""


def api_get(path: str, token: str, timeout: float = 5):
    req = urllib.request.Request(f"{API}{path}?token={token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def api_post(path: str, token: str, payload: dict, timeout: float = 300):
    req = urllib.request.Request(
        f"{API}{path}?token={token}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


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


class ChatWorker(QThread):
    """后台线程发消息，避免卡住 UI"""
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, token: str, text: str):
        super().__init__()
        self.token, self.text = token, text

    def run(self):
        try:
            self.done.emit(api_post("/api/chat", self.token, {"message": self.text})["reply"])
        except Exception as e:
            self.failed.emit(str(e))


class StatusWorker(QThread):
    """定期刷新状态徽标"""
    refreshed = Signal(dict)

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def run(self):
        try:
            self.refreshed.emit(api_get("/api/status", self.token))
        except Exception:
            pass


class SettingsDialog(QDialog):
    def __init__(self, token: str, parent=None):
        super().__init__(parent)
        self.token = token
        self.setWindowTitle("设置")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.base_url = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.model = QLineEdit()
        self.tts = QCheckBox("语音播报回复")
        form.addRow("API 接口地址", self.base_url)
        form.addRow("API Key", self.api_key)
        form.addRow("模型", self.model)
        form.addRow("", self.tts)
        layout.addLayout(form)

        save = QPushButton("保存")
        save.clicked.connect(self.save)
        layout.addWidget(save)

        try:
            cfg = api_get("/api/config", self.token)
            self.base_url.setText(cfg.get("base_url", ""))
            self.api_key.setText(cfg.get("api_key", ""))
            self.model.setText(cfg.get("model", ""))
            self.tts.setChecked(bool(cfg.get("tts_enable")))
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))

    def save(self):
        try:
            api_post("/api/config", self.token, {
                "base_url": self.base_url.text(),
                "api_key": self.api_key.text(),
                "model": self.model.text(),
                "tts_enable": self.tts.isChecked(),
            })
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))


class MainWindow(QMainWindow):
    def __init__(self, token: str):
        super().__init__()
        self.token = token
        self.worker: ChatWorker | None = None
        self.setWindowTitle("DeckMate")
        self.resize(520, 680)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 顶栏
        top = QHBoxLayout()
        self.status_label = QLabel("● 已连接")
        self.status_label.setStyleSheet("color:#2ecc71; font-size:14px;")
        self.badge = QLabel("")
        self.badge.setStyleSheet("color:#8b91a3; font-size:13px;")
        settings_btn = QPushButton("⚙ 设置")
        settings_btn.clicked.connect(self.open_settings)
        top.addWidget(self.status_label)
        top.addStretch(1)
        top.addWidget(self.badge)
        top.addWidget(settings_btn)
        layout.addLayout(top)

        # 消息区
        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(True)
        layout.addWidget(self.chat, 1)

        # 输入区
        bottom = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入消息，回车发送…")
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self.send)
        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.send_btn)
        layout.addLayout(bottom)

        self.refresh_status()

    def append_msg(self, role: str, text: str):
        if role == "user":
            color, align, name = "#1a9fff", "right", "我"
        else:
            color, align, name = "#232737", "left", "DeckMate"
        html = (
            f'<div style="text-align:{align}; margin:6px 0;">'
            f'<span style="font-size:11px; color:#8b91a3;">{name}</span><br>'
            f'<span style="display:inline-block; max-width:80%; text-align:left;'
            f' background:{color}; color:#e8eaf0; padding:8px 12px;'
            f' border-radius:12px; font-size:15px;">{text}</span></div>'
        )
        self.chat.append(html)

    def send(self):
        text = self.input.text().strip()
        if not text or (self.worker and self.worker.isRunning()):
            return
        self.input.clear()
        self.append_msg("user", text)
        self.chat.append('<span style="color:#8b91a3; font-size:13px;">DeckMate 正在输入…</span>')
        self.send_btn.setEnabled(False)
        self.worker = ChatWorker(self.token, text)
        self.worker.done.connect(self.on_reply)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_reply(self, reply: str):
        self.send_btn.setEnabled(True)
        self.append_msg("assistant", reply.replace("\n", "<br>"))

    def on_error(self, err: str):
        self.send_btn.setEnabled(True)
        self.append_msg("assistant", f"出错了：{err}")

    def open_settings(self):
        SettingsDialog(self.token, self).exec()

    def refresh_status(self):
        self.status_worker = StatusWorker(self.token)
        self.status_worker.refreshed.connect(self.on_status)
        self.status_worker.start()

    def on_status(self, s: dict):
        parts = []
        if s.get("battery") is not None:
            icon = "⚡" if s.get("charging") == "Charging" else "🔋"
            parts = [f"{icon}{s['battery']}%"]
        else:
            parts = []
        parts.append("游戏模式" if s.get("mode") == "gaming" else "桌面模式")
        self.badge.setText(" · ".join(parts))
        if not s.get("llm_configured"):
            self.append_msg("assistant", "API Key 还没填，点右上角「⚙ 设置」填上就能聊了。")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DeckMate")

    # 深色主题
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#0f1117"))
    palette.setColor(QPalette.WindowText, QColor("#e8eaf0"))
    palette.setColor(QPalette.Base, QColor("#1a1d27"))
    palette.setColor(QPalette.AlternateBase, QColor("#232737"))
    palette.setColor(QPalette.Text, QColor("#e8eaf0"))
    palette.setColor(QPalette.Button, QColor("#232737"))
    palette.setColor(QPalette.ButtonText, QColor("#e8eaf0"))
    palette.setColor(QPalette.Highlight, QColor("#1a9fff"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.PlaceholderText, QColor("#8b91a3"))
    app.setPalette(palette)
    app.setStyleSheet(
        "QLineEdit, QTextBrowser { background:#1a1d27; border:1px solid #2a2f42;"
        " border-radius:8px; padding:8px; font-size:15px; }"
        "QPushButton { background:#1a9fff; color:#fff; border:none;"
        " border-radius:8px; padding:8px 18px; font-size:15px; }"
        "QPushButton:disabled { background:#2a2f42; color:#8b91a3; }"
    )

    if not ensure_backend():
        QMessageBox.critical(None, "DeckMate", "后端核心启动失败，请检查 ~/deckmate 是否完整。")
        return 1

    win = MainWindow(load_token())
    icon_path = DECKMATE_DIR / "assets" / "logo.jpg"
    if icon_path.exists():
        win.setWindowIcon(QIcon(str(icon_path)))
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
