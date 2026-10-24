"""DeckMate 操作遮罩

agent 操作这台设备期间，在桌面上叠加一层半透明全屏遮罩，显示当前正在做什么。
用户点一下遮罩（触摸屏轻触、鼠标点击、触摸板点击均可）立刻调用 /api/cancel
终止当前操作并撤掉遮罩。

遮罩不读取任何输入设备，也不需要任何系统权限。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

API = os.environ.get("DECKMATE_API", "http://127.0.0.1:18765")
TOKEN = os.environ.get("DECKMATE_TOKEN", "")

POLL_MS = 400          # 向后端询问「是否在操作」
MIN_SHOW_MS = 700      # 最短显示时长，避免秒级工具闪一下就消失

# 遮罩进程不读 KDE 字体配置。优先等宽字体：它的空格宽度明确，
# 中文之外的字符不会挤在一起；中文由后面的 CJK 字体补上
FONT_FAMILIES = ["Noto Sans Mono", "DejaVu Sans Mono", "Noto Sans CJK SC"]


def ui_font(px: int, bold: bool = False) -> QFont:
    # 用 setFamilies 而不是 setFamily：构造 QFont() 时 family 已经被解析成
    # Sans Serif，再 setFamily 不会触发重新匹配，只能靠 setFamilies 覆盖
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class Mask(QWidget):
    def __init__(self):
        # 覆盖重定向窗口（绕过窗口管理器）：不进任务栏、始终盖在普通窗口之上。
        # 不能用 WindowTransparentForInput：遮罩本身就是用来接住用户点击的
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.X11BypassWindowManagerHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.PointingHandCursor)
        self._text = ""
        self.phase = 0

        # 卡片用 QLabel 排版，换行/对齐交给 Qt，paintEvent 只管背景和按钮
        self.card = QFrame(self)
        self.card.setObjectName("card")
        self.card.setStyleSheet(
            "#card { background: rgba(16, 20, 30, 235);"
            " border: 1px solid rgba(26, 159, 255, 120); border-radius: 16px; }")
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        self.title = QLabel()
        self.title.setFont(ui_font(19, bold=True))
        self.title.setStyleSheet("color: #e8eaf0;")
        self.title.setAlignment(Qt.AlignCenter)

        self.action = QLabel()
        self.action.setFont(ui_font(13))
        self.action.setStyleSheet("color: #8b91a3;")
        self.action.setAlignment(Qt.AlignCenter)
        self.action.setWordWrap(True)
        self.action.setFixedWidth(584)
        self.action.setMinimumHeight(20)

        self.hint = QLabel("点任意位置立即停止")
        self.hint.setFont(ui_font(14, bold=True))
        self.hint.setStyleSheet(
            "color: #fff; background: rgba(232, 74, 74, 235);"
            " border-radius: 17px; padding: 8px 20px;")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setFixedHeight(34)

        lay.addWidget(self.title)
        lay.addWidget(self.action, 0, Qt.AlignHCenter)
        lay.addSpacing(4)
        lay.addWidget(self.hint, 0, Qt.AlignHCenter)

        self._shown_at = 0.0
        self._cancelled = False

        self.anim = QTimer(self)
        self.anim.timeout.connect(self._tick)
        self.anim.start(140)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)

    # ---------- 后端状态 ----------
    def _fetch(self) -> dict:
        req = urllib.request.Request(
            f"{API}/api/overlay_state?token={TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except Exception:
            return {}

    def _set_text(self, text: str):
        self._text = text
        self.action.setText(text or "准备中")
        self._relayout()

    def _poll(self):
        state = self._fetch()
        show = bool(state.get("show"))
        self._set_text(state.get("text") or "")
        now = time.monotonic()
        if show:
            if not self.isVisible():
                self._show_mask()
                self._shown_at = now
                self._cancelled = False
        elif self.isVisible() and (now - self._shown_at) * 1000 >= MIN_SHOW_MS:
            self.hide()

    def _cancel(self):
        if self._cancelled:
            return
        self._cancelled = True
        self._set_text("已按你的干预终止操作")

        def post():
            try:
                req = urllib.request.Request(
                    f"{API}/api/cancel?token={TOKEN}", method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass
        threading.Thread(target=post, daemon=True).start()
        QTimer.singleShot(300, self.hide)

    # ---------- 交互 ----------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._cancel()

    def keyPressEvent(self, ev):
        # 遮罩不抢焦点，这里主要是保险：有键盘时按 Esc 也能停
        if ev.key() == Qt.Key_Escape:
            self._cancel()

    def resizeEvent(self, _ev):
        self._relayout()

    def _relayout(self):
        """卡片宽度固定，高度跟随文字行数：手动 setFixedHeight 后居中摆放
        （QFrame 放在顶层窗口里不会被自动布局管理）"""
        self.card.setFixedWidth(min(620, self.width() - 60))
        self.card.layout().activate()
        h = self.card.layout().sizeHint().height()
        self.card.setFixedHeight(h)
        self.card.move((self.width() - self.card.width()) // 2,
                       (self.height() - h) // 2)

    # ---------- 绘制 ----------
    def _show_mask(self):
        """覆盖重定向窗口不走 showFullScreen，直接铺满整块屏幕"""
        scr = self.screen() or QApplication.primaryScreen()
        if scr:
            self.setGeometry(scr.geometry())
        self.show()
        self.raise_()

    def _tick(self):
        if not self.isVisible():
            return
        self.phase = (self.phase + 1) % 60
        dots = "." * (1 + (self.phase // 20) % 3)
        self.title.setText(f"DeckMate 正在操作这台设备{dots}")
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            w, h = self.width(), self.height()
            # 半透明底：能看清桌面，又明显区别于正常状态
            p.fillRect(0, 0, w, h, QColor(6, 8, 14, 125))
            # 呼吸边框
            glow = 0.45 + 0.35 * abs((self.phase % 40) - 20) / 20.0
            p.setPen(QPen(QColor(26, 159, 255, int(200 * glow)), 3))
            p.drawRect(2, 2, w - 4, h - 4)
        finally:
            p.end()


def main() -> int:
    app = QApplication([])
    app.setApplicationName("DeckMateOverlay")
    app.setFont(ui_font(13))
    mask = Mask()
    mask.hide()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
