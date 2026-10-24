"""DeckMeido 操作遮罩

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

API = os.environ.get("DECKMEIDO_API", "http://127.0.0.1:18765")
TOKEN = os.environ.get("DECKMEIDO_TOKEN", "")

POLL_MS = 100          # 向后端询问「是否在操作」的常规间隔
URGENT_MS = 25         # 穿透期间的快速轮询间隔（后端马上要点，等不起）
LIFT_MS = 25           # 截图期间抬高遮罩时的轮询间隔
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
        self._passthrough = False
        self._lifted = False
        self._show_wanted = False

        self.anim = QTimer(self)
        self.anim.timeout.connect(self._tick)
        self.anim.start(140)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)
        # 后端要点击时会立刻打开 passthrough 然后马上点，
        # 常规 400ms 轮询来不及反应，所以这里记下「需要快速轮询」的状态
        self._urgent = False
        self.urgent_timer = QTimer(self)
        self.urgent_timer.timeout.connect(self._poll_urgent)
        self.urgent_timer.start(URGENT_MS)

    # ---------- 后端状态 ----------
    def _fetch(self) -> dict:
        # ack=1 告诉后端「本次返回的状态我已经照做了」，后端据此判断可以安全点击
        req = urllib.request.Request(
            f"{API}/api/overlay_state?ack=1&token={TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except Exception:
            return {}

    def _set_text(self, text: str):
        self._text = text
        self.action.setText(text or "准备中")
        self._relayout()

    def _poll_urgent(self):
        """穿透期间每 25ms 问一次后端，保证 agent 点击前遮罩已经让开"""
        if self._urgent:
            self._poll()

    def _poll(self):
        state = self._fetch()
        show = bool(state.get("show"))
        self._show_wanted = show
        self._set_text(state.get("text") or "")
        now = time.monotonic()
        # agent 发送鼠标点击期间后端会打开穿透，否则那一下会点在遮罩上变成「用户终止」
        self._set_passthrough(bool(state.get("passthrough")))
        # 截图期间后端要求把遮罩藏起来，免得挡住要拍的画面
        self._set_lifted(bool(state.get("lift")))
        # 正在显示、刚要求穿透、或刚要求抬高遮罩时，切到快速轮询
        self._urgent = bool(show or self._passthrough or self._lifted)
        # 显示条件里不能再排除 passthrough：穿透只是不接鼠标，画面仍要留在屏幕上
        if show:
            if not self.isVisible() and not self._lifted:
                self._show_mask()
                self._shown_at = now
                self._cancelled = False
        elif self.isVisible() and (now - self._shown_at) * 1000 >= MIN_SHOW_MS:
            self.hide()

    def _set_passthrough(self, on: bool):
        if on == self._passthrough:
            return
        self._passthrough = on
        # 穿透期间只让鼠标事件透过，窗口本身不能藏掉：遮罩要一直显示「正在操作」，
        # 用户也还能看到进度。用属性而不是 hide()，重绘时保留半透明底
        self.setAttribute(Qt.WA_TransparentForMouseEvents, on)
        if not on and self._show_wanted and not self._lifted and not self.isVisible():
            self._show_mask()
            self._shown_at = time.monotonic()
        self.repaint()

    def _set_lifted(self, on: bool):
        """截图期间把遮罩藏起来，拍完立刻恢复，不影响「点一下终止」"""
        if on == self._lifted:
            return
        self._lifted = on
        if on:
            self.hide()
        elif self._show_wanted and not self._passthrough:
            self._show_mask()
            self._shown_at = time.monotonic()

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
        self.title.setText(f"DeckMeido 正在操作这台设备{dots}")
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
    app.setApplicationName("DeckMeidoOverlay")
    app.setFont(ui_font(13))
    mask = Mask()
    mask.hide()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
