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
    QApplication, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
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


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(text: str) -> str:
    """轻量 markdown → QTextBrowser 支持的 HTML 子集"""
    import re

    s = html_escape(text)
    s = re.sub(
        r"```\w*\n?(.*?)```",
        lambda m: f"<pre style='background:#0b0d13; padding:8px; border-radius:6px;'>{m.group(1).strip()}</pre>",
        s, flags=re.S,
    )
    s = re.sub(r"`([^`\n]+)`",
               r"<code style='background:#0b0d13; padding:1px 5px; border-radius:4px;'>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?m)^#{1,4}\s+(.+)$", r"<b style='color:#1a9fff;'>\1</b>", s)
    s = re.sub(r"(?m)^\s*[-*] (.+)$", r"• \1", s)
    return s.replace("\n", "<br>")


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
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, token: str, text: str):
        super().__init__()
        self.token, self.text = token, text

    def run(self):
        try:
            self.done.emit(api_post("/api/chat", self.token, {"message": self.text}))
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
        self.perm = QComboBox()
        self.perm.addItem("手动审批（每条命令都问你）", "manual")
        self.perm.addItem("智能审批（危险命令才问你）", "auto")
        self.perm.addItem("完全访问（不再询问）", "full")
        form.addRow("API 接口地址", self.base_url)
        form.addRow("API Key", self.api_key)
        form.addRow("模型", self.model)
        form.addRow("控制权限", self.perm)
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
            idx = self.perm.findData(cfg.get("permission_mode", "auto"))
            if idx >= 0:
                self.perm.setCurrentIndex(idx)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))

    def save(self):
        try:
            api_post("/api/config", self.token, {
                "base_url": self.base_url.text(),
                "api_key": self.api_key.text(),
                "model": self.model.text(),
                "tts_enable": self.tts.isChecked(),
                "permission_mode": self.perm.currentData(),
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
            body = md_to_html(text)
        else:
            color, align, name = "#232737", "left", "DeckMate"
            body = md_to_html(text)
        html = (
            f'<div style="text-align:{align}; margin:6px 0;">'
            f'<span style="font-size:11px; color:#8b91a3;">{name}</span><br>'
            f'<span style="display:inline-block; max-width:80%; text-align:left;'
            f' background:{color}; color:#e8eaf0; padding:8px 12px;'
            f' border-radius:12px; font-size:15px;">{body}</span></div>'
        )
        self.chat.append(html)

    def append_events(self, events: list):
        """渲染思维链 / 工具调用 / 文件改动"""
        for ev in events or []:
            if ev.get("type") == "thought":
                text = html_escape(ev.get("text", ""))[:800]
                self.chat.append(
                    f'<div style="color:#6b7280; font-size:12px; font-style:italic;'
                    f' margin:2px 0;">🧠 {text}</div>'
                )
            elif ev.get("type") == "tool":
                icon = "✓" if ev.get("approved") else "🔧"
                summary = html_escape(ev.get("summary", ""))
                self.chat.append(
                    f'<div style="font-size:12.5px; color:#9fd4ff; margin:2px 0;">'
                    f'{icon} <b style="color:#1a9fff;">{ev.get("name")}</b> {summary}</div>'
                )
                out = html_escape(ev.get("output", "") or "")
                if out:
                    self.chat.append(
                        f'<pre style="background:#0b0d13; color:#c8d3e0; font-size:12px;'
                        f' padding:8px; border-radius:6px; margin:2px 0 4px 12px;">{out}</pre>'
                    )
                if ev.get("image"):
                    url = f"http://127.0.0.1:18765{ev['image']}?token={self.token}"
                    self.chat.append(
                        f'<div style="margin:2px 0 4px 12px;">'
                        f'<a href="{url}" style="color:#1a9fff;">📷 查看截图</a></div>'
                    )
                for f in ev.get("files") or []:
                    self.chat.append(
                        f'<div style="font-size:12px; color:#7ddba3; margin:0 0 2px 18px;">'
                        f'📄 写入 {html_escape(str(f))}</div>'
                    )

    def send(self):
        # 忙碌中点击 = 终止当前操作
        if self.worker and self.worker.isRunning():
            try:
                api_post("/api/cancel", self.token, {}, timeout=5)
            except Exception:
                pass
            self.send_btn.setText("发送")
            self.send_btn.setStyleSheet("")
            return
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.append_msg("user", text)
        self.chat.append('<span style="color:#8b91a3; font-size:13px;">DeckMate 正在输入…</span>')
        self.send_btn.setText("⏹")
        self.send_btn.setStyleSheet("background:#e8463a;")
        self.worker = ChatWorker(self.token, text)
        self.worker.done.connect(self.on_reply)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_reply(self, res: dict):
        self.send_btn.setText("发送")
        self.send_btn.setStyleSheet("")
        self.append_events(res.get("events"))
        self.append_msg("assistant", res.get("reply", ""))

    def on_error(self, err: str):
        self.send_btn.setText("发送")
        self.send_btn.setStyleSheet("")
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
