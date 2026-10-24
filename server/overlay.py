"""DeckMate 操作遮罩

agent 操作这台设备期间，在桌面上叠加一层半透明全屏遮罩（鼠标可穿透，不影响 agent
自己的点击）。同时监听真实的物理输入设备：一旦检测到鼠标 / 触摸板移动或触摸，
立刻调用 /api/cancel 终止当前操作并撤掉遮罩。

用 evdev 读取物理设备而不是查询 X 光标位置，是因为 agent 自己用 xdotool 移动指针
时走 XTEST 合成事件，不会出现在 /dev/input 里，不会被误判成用户干预。
设备不可读时退回 X 光标轮询。
"""
from __future__ import annotations

import ctypes
import glob
import os
import select
import struct
import threading
import time
import urllib.request

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

API = os.environ.get("DECKMATE_API", "http://127.0.0.1:18765")
TOKEN = os.environ.get("DECKMATE_TOKEN", "")

POLL_MS = 400          # 向后端询问「是否在操作」
MIN_SHOW_MS = 700      # 最短显示时长，避免秒级工具闪一下就消失
GRACE_MS = 350         # 遮罩刚出现时的忽略窗口，避免把触发前的残余动作算进去
MOVE_THRESHOLD = 6     # X 光标回退方案：累计移动像素

EVENT_SIZE = struct.calcsize("llHHI")

# EV_REL=2 REL_X=0 REL_Y=1 / EV_ABS=3 ABS_X=0 ABS_Y=1 ABS_MT_POSITION_X=53 Y=54
# EV_KEY=1 BTN_TOUCH=330 BTN_TOOL_FINGER=325 BTN_LEFT=272
_REL_CODES = {0, 1}
_ABS_CODES = {0, 1, 53, 54}
_KEY_CODES = {272, 325, 330}


class Mask(QWidget):
    def __init__(self):
        # 覆盖重定向窗口（绕过窗口管理器）：不进任务栏、不抢焦点、始终盖在普通窗口之上
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
            | Qt.X11BypassWindowManagerHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.BlankCursor)
        self.text = ""
        self.phase = 0
        self._shown_at = 0.0
        self.trigger = threading.Event()
        self._cancelled = False

        self._watch_started = False
        watcher = self._watch_x_pointer if os.environ.get("DECKMATE_POINTER_ONLY") == "1" \
            else self._watch_inputs
        threading.Thread(target=watcher, daemon=True).start()

        self.anim = QTimer(self)
        self.anim.timeout.connect(self._tick)
        self.anim.start(140)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)

    # ---------- 输入监听 ----------
    def _watch_inputs(self):
        """优先监听物理输入设备；不可用时退回 X 光标轮询"""
        devs = []
        for p in sorted(glob.glob("/dev/input/event*")):
            try:
                devs.append(open(p, "rb", buffering=0))
            except Exception:
                continue
        if not devs:
            print("输入设备不可读，回退到 X 光标轮询", flush=True)
            self._watch_x_pointer()
            return
        self._watch_started = True
        print(f"监听物理输入设备 {len(devs)} 个", flush=True)
        while True:
            try:
                ready, _, _ = select.select(devs, [], [], 0.5)
            except Exception:
                return
            for f in ready:
                try:
                    data = f.read(EVENT_SIZE)
                except Exception:
                    continue
                if not data or len(data) < EVENT_SIZE:
                    continue
                try:
                    _s, _u, etype, code, value = struct.unpack("llHHI", data)
                except Exception:
                    continue
                moved = (
                    (etype == 2 and code in _REL_CODES and value != 0)
                    or (etype == 3 and code in _ABS_CODES)
                    or (etype == 1 and code in _KEY_CODES and value == 1)
                )
                if moved:
                    self.trigger.set()

    def _watch_x_pointer(self):
        """回退方案：轮询 X 光标位置"""
        try:
            lib = ctypes.CDLL("libX11.so.6")
        except Exception:
            return
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XQueryPointer.restype = ctypes.c_int
        lib.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]

        dpy = lib.XOpenDisplay(None)
        if not dpy:
            return
        root = lib.XDefaultRootWindow(dpy)
        win = ctypes.c_ulong()
        rx, ry = ctypes.c_int(), ctypes.c_int()
        wx, wy = ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()

        def pointer():
            ok = lib.XQueryPointer(dpy, root, ctypes.byref(win), ctypes.byref(win),
                                   ctypes.byref(rx), ctypes.byref(ry),
                                   ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
            return (rx.value, ry.value) if ok else None

        base = pointer()
        while True:
            time.sleep(0.15)
            cur = pointer()
            if not cur:
                continue
            if base is None:
                base = cur
                continue
            if abs(cur[0] - base[0]) + abs(cur[1] - base[1]) >= MOVE_THRESHOLD:
                self.trigger.set()
                base = cur

    # ---------- 后端状态 ----------
    def _fetch(self) -> dict:
        req = urllib.request.Request(
            f"{API}/api/overlay_state?token={TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                import json
                return json.loads(r.read())
        except Exception:
            return {}

    def _poll(self):
        state = self._fetch()
        show = bool(state.get("show"))
        self.text = state.get("text") or ""
        now = time.time()
        if show:
            if not self.isVisible():
                self._show_mask()
                self._shown_at = now
                self.trigger.clear()   # 丢弃遮罩出现前积累的动作
                self._cancelled = False
            else:
                self.update()
        elif self.isVisible() and (now - self._shown_at) * 1000 >= MIN_SHOW_MS:
            self.hide()

        if self.trigger.is_set():
            self.trigger.clear()
            if self.isVisible() and (now - self._shown_at) * 1000 >= GRACE_MS:
                self._cancel()

    def _cancel(self):
        if self._cancelled:
            return
        self._cancelled = True
        self.text = "已按你的干预终止操作"
        self.update()

        def post():
            try:
                req = urllib.request.Request(
                    f"{API}/api/cancel?token={TOKEN}", method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass
        threading.Thread(target=post, daemon=True).start()
        QTimer.singleShot(300, self.hide)

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
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        # 半透明底：能看清桌面，又明显区别于正常状态
        p.fillRect(0, 0, w, h, QColor(6, 8, 14, 125))

        # 呼吸边框
        glow = 0.45 + 0.35 * abs((self.phase % 40) - 20) / 20.0
        p.setPen(QPen(QColor(26, 159, 255, int(200 * glow)), 3))
        p.drawRect(2, 2, w - 4, h - 4)

        # 中央卡片
        card_w, card_h = min(520, w - 60), 132
        cx, cy = (w - card_w) / 2, h / 2 - card_h / 2
        path = QPainterPath()
        path.addRoundedRect(QRectF(cx, cy, card_w, card_h), 16, 16)
        p.fillPath(path, QColor(16, 20, 30, 235))
        p.setPen(QPen(QColor(26, 159, 255, 120), 1))
        p.drawPath(path)

        p.setPen(QColor(232, 234, 240))
        f = QFont()
        f.setPixelSize(19)
        f.setBold(True)
        p.setFont(f)
        dots = "." * (1 + (self.phase // 20) % 3)
        p.drawText(QRectF(cx, cy + 18, card_w, 26), Qt.AlignCenter,
                   f"DeckMate 正在操作这台设备{dots}")

        p.setPen(QColor(139, 145, 163))
        f2 = QFont()
        f2.setPixelSize(13)
        p.setFont(f2)
        action = self.text or "准备中"
        if len(action) > 48:
            action = action[:48] + "…"
        p.drawText(QRectF(cx + 18, cy + 52, card_w - 36, 22), Qt.AlignCenter, action)

        p.setPen(QColor(26, 159, 255))
        f3 = QFont()
        f3.setPixelSize(14)
        f3.setBold(True)
        p.setFont(f3)
        p.drawText(QRectF(cx, cy + 84, card_w, 26), Qt.AlignCenter,
                   "移动鼠标或触摸板即可立即终止")
        p.end()


def main() -> int:
    app = QApplication([])
    app.setApplicationName("DeckMateOverlay")
    mask = Mask()
    mask.hide()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
