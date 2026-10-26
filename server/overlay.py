"""DeckMeido 操作遮罩

agent 操作这台设备期间，在桌面上叠加一层半透明全屏遮罩，显示当前正在做什么。
用户点一下遮罩（触摸屏轻触、鼠标点击、触摸板点击均可）、或者按一下音量键，
立刻调用 /api/cancel 终止当前操作并撤掉遮罩。

音量键监听只在遮罩显示期间生效：遮罩一出现就独占（grab）Steam Controller
的输入节点，此时音量键不再调音量、改成「强制停止」；遮罩一消失立刻松手
（ungrab），音量键恢复原样。独占期间读到的事件一律不再转发给系统。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import select
import signal
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPointF, QEvent, QCoreApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QLabel, QVBoxLayout, QWidget,
)


class _StopRequested(QEvent):
    """监听线程 → 主线程：请把当前任务停掉

    用自定义 QEvent 而不是 QTimer.singleShot：后者要求调用线程有 Qt 事件循环，
    监听线程没有；postEvent 则是线程安全的，事件会排进接收者的线程队列。
    """
    TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self):
        super().__init__(self.TYPE)


ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_PX = 152     # 卡片上方 logo 的显示边长
LOGO_GAP = 14     # logo 图形与卡片之间的实际间距（按可见图形算，不含透明留白）
LOGO_DY = 10      # logo+卡片的整体在垂直居中基础上额外下移的像素
SWAY_DEG = 9.0    # logo 绕中心左右各摆多少度
SWAY_RISE = 4.0   # 摆动同时上下浮动的像素幅度
# 摆动时图形会转出去一点，画布按对角线留余量，否则两侧边缘会被切掉
LOGO_BOX = int(LOGO_PX * math.sqrt(2)) + 8

API = os.environ.get("DECKMEIDO_API", "http://127.0.0.1:18765")
TOKEN = os.environ.get("DECKMEIDO_TOKEN", "")

POLL_MS = 100          # 向后端询问「是否在操作」的常规间隔
URGENT_MS = 25         # 快速轮询间隔：遮罩显示中 / 要让开 / 要抬高 / agent 马上要按键
MIN_SHOW_MS = 700      # 最短显示时长，避免确认类动作（点一下就完）闪一下就消失

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


class SwayLogo(QWidget):
    """会摆动的 logo。

    QLabel 只会原样贴图，转不了；这里自己画 pixmap：
    以可见图形的中心为轴心旋转 ang 度，再整体上下偏移 dy 像素。
    窗口尺寸固定为 LOGO_PX，旋转后四角稍微溢出，所以绘制时按对角线
    长度缩放一次，保证任何角度都不会被裁掉。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm = QPixmap()
        self._ang = 0.0
        self._dy = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_pixmap(self, pm: QPixmap):
        self._pm = pm
        # 画布只留旋转/浮动需要的余量：旋转会往外探出，按对角线算；
        # 但只在图形那一侧留，不要留成一个大正方形（否则会把间距撑得很大）
        if pm.isNull():
            self.setFixedSize(LOGO_BOX, LOGO_BOX)
        else:
            self.setFixedSize(
                int(pm.width() * math.sqrt(2)) + 8,
                int(pm.height() + 2 * SWAY_RISE) + 8)
        self.update()

    def box_height(self) -> int:
        return self.height() if not self._pm.isNull() else 0

    def pixmap(self) -> QPixmap:
        return self._pm

    def isNull(self) -> bool:
        return self._pm.isNull()

    def set_sway(self, ang: float, dy: float):
        self._ang = ang
        self._dy = dy
        self.update()

    def reset_sway(self):
        self._ang = 0.0
        self._dy = 0.0
        self.update()

    def paintEvent(self, _ev):
        if self._pm.isNull():
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            # 窗口留了旋转/浮动余量（见 LOGO_BOX），这里按可见边长原尺寸画，
            # 以实际图形中心为轴心转，摆到任何角度都不会出界
            w = self._pm.width()
            h = self._pm.height()
            cx = self.width() / 2.0
            cy = self.height() / 2.0 + self._dy
            p.translate(cx, cy)
            p.rotate(self._ang)
            p.translate(-cx, -cy)
            p.drawPixmap(QPointF(cx - w / 2.0, cy - h / 2.0), self._pm)
        finally:
            p.end()


class VolumeKeyStop:
    """遮罩显示期间，把音量键变成「强制停止」

    为什么不是简单注册个快捷键：音量键是普通 evdev 设备上的硬件键，读它而不抓住
    它，系统照样会调音量，遮罩期间就成了「既停任务又改音量」。而 evdev 的一个设备
    同时只能被一个进程独占（grab），独占期间读到的事件也**不会**再进系统。
    所以本类的策略是：
      - 遮罩显示 → grab，读到的音量键当停止键，其它事件丢弃（不回放）
      - 遮罩消失 → ungrab，音量键立刻恢复原样
    代价是遮罩期间音量真的调不了，这是「只在遮罩时监听」的必然结果，
    换来的是松手即恢复、不会像常驻占用那样把音量键永久弄坏。

    Steam Deck 上音量键**不在** Steam Controller 节点上，而是走
    `AT Translated Set 2 keyboard`（键盘节点）。不同机型/系统版本挂在哪都可能变，
    所以不按名字找：扫 /dev/input/event*，用 capabilities 位图筛出真的声明了
    KEY_VOLUMEUP/DOWN 的设备，再逐个 probe 哪个真能读到事件。

    安全性：`_release()` 与 grab 成对，监听线程是 daemon，遮罩进程退出时内核
    会随文件描述符关闭自动释放独占，不会把音量键留在「按了没反应」的状态。
    """

    VOLUME_KEYS = (114, 115)          # KEY_VOLUMEDOWN / KEY_VOLUMEUP
    _EV_KEY = 0x01
    _KEY_PRESS = 1
    _EVENT = struct.Struct("llHHi")   # struct input_event（64 位下 24 字节）

    def __init__(self, on_stop):
        self._on_stop = on_stop
        self._lock = threading.Lock()
        self._want = False
        self._fd: int | None = None
        self._wake_r: int | None = None
        self._wake_w: int | None = None
        self._thread = threading.Thread(
            target=self._run, name="volume-key-stop", daemon=True)

    # ---------- 对外 ----------
    def set_active(self, active: bool) -> None:
        """遮罩显示/消失时调用；可反复调用，只在真正变化时才动设备"""
        with self._lock:
            if active == self._want:
                return
            self._want = active
            wake = self._wake_w
        if wake is not None:
            try:
                os.write(wake, b"\x01")   # 让 select 立刻醒来，切到新状态
            except OSError:
                pass

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        """进程退出前同步松手，别把音量键锁死"""
        self.set_active(False)
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)

    # ---------- 内部 ----------
    @classmethod
    def _volume_devices(cls) -> list[str]:
        """声明了音量键的输入节点，最可能真发音量键的排前面

        有两个坑，都是实机踩出来的：
        1. `capabilities/key` 是声明，不是事实。Deck 上 Steam Controller 的两个
           节点都声明了 KEY_VOLUMEUP，但按下音量键读到事件的是
           `AT Translated Set 2 keyboard`——内置键盘节点。
        2. 所以先按 sysfs 设备名把键盘类节点提上来（名字以 keyboard 结尾），
           剩下的伪节点只作兜底；真伪最终由 `_open` 里的 probe 定。
        选择顺序变了但兜底逻辑不变：probe 全失败就当作没有音量键，遮罩照常能用。
        """
        try:
            names = sorted(os.listdir("/dev/input"))
        except OSError:
            return []
        real, other = [], []
        for name in names:
            if not name.startswith("event"):
                continue
            dev = Path("/sys/class/input") / name / "device"
            if not cls._has_volume_keys(dev / "capabilities/key"):
                continue
            try:
                label = (dev / "name").read_text().strip()
            except OSError:
                label = ""
            (real if "keyboard" in label.lower() else other).append(
                f"/dev/input/{name}")
        return real + other

    @classmethod
    def _has_volume_keys(cls, cap: Path) -> bool:
        """解析 capabilities/key 的位图，确认设备声明了音量键

        sysfs 里这张位图是 **hex 字节串**（形如 `feffffdfffcfffff`），
        取值要按字节倒序当大整数读，不能按 64 位字拼接——按字拼出来的结果
        会把 K 附近整片位都点亮，于是键盘节点（真正发音量键的那个）反而被判成
        「没有音量键」，而 Steam Controller 那些死节点被判成「有」。
        """
        try:
            raw = cap.read_text().split()
        except OSError:
            return False
        val = 0
        for word in raw:
            val = (val << (4 * len(word))) | int(word, 16)
        return any(val >> k & 1 for k in cls.VOLUME_KEYS)

    def _open(self) -> bool:
        """独占第一个能打开、能 grab 的候选节点

        顺序由 `_volume_devices` 决定（键盘类节点优先）。不能在这里「等一小会看
        有没有事件」来判活：用户没按的时候所有节点都是安静的，任何按时间窗的
        probe 都会把好节点也否掉，遮罩期间就变成按了没反应。
        """
        for path in self._volume_devices():
            try:
                import fcntl
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except (ImportError, OSError):
                continue
            # 遮罩在 XWayland 里跑，属于 deck 用户，本身就在 input 组里；
            # 万一没权限 EVIOCGRAB 会失败，此时跳过这个节点
            try:
                fcntl.ioctl(fd, 0x40044590, 1)   # EVIOCGRAB
            except OSError:
                os.close(fd)
                continue
            self._fd = fd
            return True
        return False

    def _release(self) -> None:
        """放开独占：关掉 fd 就是 ungrab（独占随最后一个引用释放而解除）"""
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def _run(self) -> None:
        """监听线程主体：常驻，按 _want 在「独占读」与「松手」之间切换"""
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        while True:
            try:
                fds = [self._wake_r] + ([self._fd] if self._fd is not None else [])
                ready, _, _ = select.select(fds, [], [], 0.5)
                for fd in ready:
                    if fd == self._wake_r:
                        self._drain_wake()
                    else:
                        self._drain_events()
                with self._lock:
                    want = self._want
                if want and self._fd is None:
                    self._open()
                elif not want and self._fd is not None:
                    self._release()
            except Exception:
                self._release()   # 出任何岔子先松手，别把音量键锁死
                time.sleep(0.2)

    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_r, 64):
                pass
        except OSError:
            pass

    def _drain_events(self) -> None:
        while self._fd is not None:
            try:
                data = os.read(self._fd, self._EVENT.size * 64)
            except BlockingIOError:
                return
            except OSError:
                return
            if not data:
                return
            for off in range(0, len(data) - self._EVENT.size + 1, self._EVENT.size):
                _s, _us, typ, code, value = self._EVENT.unpack_from(data, off)
                if typ == self._EV_KEY and value == self._KEY_PRESS \
                        and code in self.VOLUME_KEYS:
                    self._on_stop()


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
        # 固定文案：不再跟着计时器变「…」，动态感交给下方 logo 的摆动
        self.title.setText("DeckMeido 正在思考")

        self.action = QLabel()
        self.action.setFont(ui_font(13))
        self.action.setStyleSheet("color: #8b91a3;")
        self.action.setAlignment(Qt.AlignCenter)
        self.action.setWordWrap(True)
        self.action.setFixedWidth(584)
        self.action.setMinimumHeight(20)

        self.hint = QLabel("点任意位置或按音量键立即停止")
        self.hint.setFont(ui_font(14, bold=True))
        self.hint.setStyleSheet(
            "color: #fff; background: rgba(232, 74, 74, 235);"
            " border-radius: 17px; padding: 8px 20px;")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setFixedHeight(34)

        # logo 独立于卡片，浮在卡片正上方（进卡片会撑高卡片、挤乱排版）
        # 用自绘的 SwayLogo 而不是 QLabel：思考时要让它绕中心摆动
        self.logo = SwayLogo(self)
        self.logo.set_pixmap(self._load_logo())
        self._sway_t = 0.0

        lay.addWidget(self.title)
        lay.addWidget(self.action, 0, Qt.AlignHCenter)
        lay.addSpacing(4)
        lay.addWidget(self.hint, 0, Qt.AlignHCenter)

        self._shown_at = 0.0
        self._cancelled = False
        self._passthrough = False
        # 后端预告「下一步会按鼠标键」：和 passthrough 一样要让开，区别是它由
        # 后端在显示遮罩之前就置上，本进程一看到就要立刻藏（见 _poll）
        self._pointer = False
        self._input_transparent = False
        self._lifted = False
        self._show_wanted = False

        self.anim = QTimer(self)
        self.anim.timeout.connect(self._tick)
        # 摆动动画要流畅，140ms 一步会明显一顿一顿；33ms ≈ 30fps，
        # 只重画一个小小的 logo 控件，开销可以忽略
        self.anim.start(33)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)
        # 后端要点击时会立刻打开 passthrough 然后马上点，
        # 常规 400ms 轮询来不及反应，所以这里记下「需要快速轮询」的状态
        self._urgent = False
        self.urgent_timer = QTimer(self)
        self.urgent_timer.timeout.connect(self._poll_urgent)
        self.urgent_timer.start(URGENT_MS)

        # 音量键当「强制停止」：只在遮罩真的显示时独占设备，见 VolumeKeyStop
        self.volume_stop = VolumeKeyStop(self._on_volume_stop)
        self.volume_stop.start()

    # ---------- 后端状态 ----------
    def _fetch(self, ack: bool = True) -> dict:
        # ack=1 告诉后端「本次返回的状态我已经照做了」，后端据此判断可以安全点击。
        # hidden 自报当前是否真的已经藏好：后端要求让开时只认这个，不看它自己的意图
        q = f"ack=1&hidden={0 if self.isVisible() else 1}&" if ack else ""
        req = urllib.request.Request(
            f"{API}/api/overlay_state?{q}token={TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except Exception:
            return {}

    def _ack_state(self):
        """把「窗口真的藏好了没有」回报给后端，让它放心点下去

        这一次请求纯粹是回报，不取状态（ack 不带 hidden 参数时后端不认）。
        QWidget.isVisible() 在 hide() 后立刻为 False；X11 那边取消映射是异步的，
        所以再等两帧（约 30ms）让合成器把这一层撤下去，避免点击抢在隐藏生效前。
        """
        def report():
            try:
                req = urllib.request.Request(
                    f"{API}/api/overlay_state?ack=1&hidden="
                    f"{0 if self.isVisible() else 1}&token={TOKEN}")
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass
        QTimer.singleShot(30, lambda: threading.Thread(
            target=report, daemon=True).start())

    def _set_text(self, text: str):
        self._text = text
        new = text or "准备中"
        # 轮询每 100ms 一次，文字多数时候没变。不判重的话每轮都 setText + 重新排
        # 版，文字区会一直轻微跳动（行数一变整块卡片高度也跟着变）
        if self.action.text() == new:
            return
        self.action.setText(new)
        self._relayout()

    def _load_logo(self) -> QPixmap:
        """加载 assets/logo.png；缺失时留空 pixmap，遮罩照常工作。

        logo 图四周带透明留白（原图 512 里图形只占 y≈90~381，下方约 1/4 是空的）。
        这里直接按可见范围裁掉留白：摆动是绕「图形中心」转的，留着大片透明边
        会让它绕着一个看不见的点画圈；裁掉后 _relayout 的间距计算也更直观。
        """
        self._logo_inset_top = 0
        self._logo_inset_bottom = 0
        path = ASSETS_DIR / "logo.png"
        pm = QPixmap(str(path)) if path.exists() else QPixmap()
        if pm.isNull():
            return pm
        pm = pm.scaled(
            LOGO_PX, LOGO_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # 注意 _visual_insets 返回的是「上留白、下留白」，不是边界坐标
        inset_top, inset_bottom = self._visual_insets(pm)
        vis_h = pm.height() - inset_top - inset_bottom
        if vis_h > 0 and (inset_top or inset_bottom):
            pm = pm.copy(0, inset_top, pm.width(), vis_h)
        return pm

    @staticmethod
    def _visual_insets(pm: QPixmap) -> tuple[int, int]:
        """可见图形相对整张 pixmap 的上下留白（像素）。

        只在启动时扫一遍 152×152，成本可忽略；全透明时返回 (0, 0) 退化成整图排版。"""
        img = pm.toImage()
        w, h = img.width(), img.height()
        top = bottom = None
        for y in range(h):
            for x in range(w):
                if img.pixelColor(x, y).alpha() > 24:
                    if top is None:
                        top = y
                    bottom = y
                    break
        if top is None:
            return 0, 0
        return top, h - 1 - bottom

    def _poll_urgent(self):
        """穿透期间每 25ms 问一次后端，保证 agent 点击前遮罩已经藏好"""
        if self._urgent:
            self._poll()

    def _poll(self):
        state = self._fetch()
        show = bool(state.get("show"))
        self._show_wanted = show
        self._set_text(state.get("text") or "")
        now = time.monotonic()
        # agent 发送鼠标点击期间后端会打开穿透，否则那一下会点在遮罩上变成「用户终止」
        self._pointer = bool(state.get("pointer"))
        self._set_passthrough(bool(state.get("passthrough")) or self._pointer)
        # 截图期间后端要求把遮罩藏起来，免得挡住要拍的画面
        self._set_lifted(bool(state.get("lift")))
        # 正在显示、刚要求穿透、或刚要求抬高遮罩时，切到快速轮询
        self._urgent = bool(show or self._passthrough or self._lifted)

        if self._pointer:
            # pointer 是「马上要按下去」，只认「现在没显示」：刚播完一轮遮罩
            # 还在最短显示时间里也要立刻藏，否则这一下点会被吃掉
            if self.isVisible():
                self.hide()
            self._ack_state()
            return
        # 显示条件里不能再排除 passthrough：那只是「暂时藏起来」，
        # 注入结束、show 还没变 false 时要能把遮罩放回去
        if show:
            if not self.isVisible() and not self._lifted and not self._passthrough:
                self._show_mask()
                self._shown_at = now
                self._cancelled = False
        elif self.isVisible() and (now - self._shown_at) * 1000 >= MIN_SHOW_MS:
            self.hide()
        # 处理好之后再回报「是否真的藏好了」，后端等的是这个确认
        if self._passthrough:
            self._ack_state()

    def _set_passthrough(self, on: bool):
        if on == self._passthrough:
            return
        self._passthrough = on
        # agent 注入鼠标期间，遮罩必须整块藏起来，不能只靠窗口标志让开。
        #
        # 踩过的坑：这里原本用 WA_TransparentForMouseEvents 再用
        # WindowTransparentForInput 让 X 把窗口从命中测试里摘掉。但遮罩跑在
        # XWayland 里（QT_QPA_PLATFORM=xcb，上层是 KDE Wayland），点击是
        # Wayland 合成器做命中测试后再投给 XWayland 窗口的，X11 那层的
        # "input shape" 对合成器不可见。于是 agent 自己的 uinput/xdotool 点击
        # 照样打在遮罩上 → /api/cancel → 任务被自己的操作终止（日志里
        # 「正在操作桌面」后紧跟着一条 POST /api/cancel 就是这个）。
        # 隐藏窗口是唯一不依赖平台细节的解法：没有窗口，就没有能被打中的目标。
        # 注入只持续几十毫秒到一两秒，用户看到的是遮罩短暂消失再回来。
        # 另外 WA_TransparentForMouseEvents 仍然保留：即使 hide 与合成器撤层之间
        # 有几毫秒窗口还在，Qt 也会把这层收到的点击丢掉，不会走到 _cancel
        self.setAttribute(Qt.WA_TransparentForMouseEvents, on)
        if on:
            self.hide()
        elif self._show_wanted and not self._lifted:
            self._show_mask()
            self._shown_at = time.monotonic()


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
    def _on_volume_stop(self):
        """音量键回调：来自监听线程，交回 Qt 主线程再动界面

        不能用 QTimer.singleShot：它在调用线程找一个有事件循环的 QThread，
        而监听线程是纯 Python 线程、没有 Qt 事件循环，定时器就是死的——
        实测按键能读到（日志有 stop by key），但 _cancel 一次都没执行。
        改走事件总线：postEvent 是线程安全的，事件会排进主线程的事件队列。
        """
        QCoreApplication.postEvent(self, _StopRequested())

    def event(self, ev):
        # 音量键的停止请求从监听线程投递过来，在这里回到主线程执行
        if ev.type() == _StopRequested.TYPE:
            self._cancel()
            return True
        return super().event(ev)

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
        （QFrame 放在顶层窗口里不会被自动布局管理）。

        logo 与卡片作为一个整体垂直居中。间距按 logo 的「可见图形」算：
        logo 图片自带透明留白，若按整张 pixmap 占位，参数 14px 会被撑成 50+px，
        看着就像两块东西离得很远。"""
        self.card.setFixedWidth(min(620, self.width() - 60))
        self.card.layout().activate()
        h = self.card.layout().sizeHint().height()
        self.card.setFixedHeight(h)

        has_logo = not self.logo.isNull()
        # 可见图形高度 = 裁掉留白后的 pixmap 高度（SwayLogo 里按原尺寸居中绘制）
        pm = self.logo.pixmap()
        vis_h = pm.height() if has_logo else 0
        # 画布高度可选：按图形高度 + 旋转/浮动余量，避免留一大块透明底把
        # 眼睛看到的间距撑大
        box_h = self.logo.box_height()
        gap = LOGO_GAP if has_logo else 0
        total = (box_h + gap if has_logo else 0) + h
        top = (self.height() - total) // 2 + LOGO_DY

        if has_logo:
            self.logo.show()
            self.logo.move((self.width() - self.logo.width()) // 2, top)
            top += box_h + gap
        else:
            self.logo.hide()

        self.card.move((self.width() - self.card.width()) // 2, top)

    # ---------- 绘制 ----------
    def _sync_volume_grab(self):
        """音量键独占跟随「遮罩是否真的显示」

        不在遮罩时绝不占用：用户平时调音量、系统 OSD 都照旧。
        穿透（agent 自己在点）与截图抬高期间遮罩是藏起来的，这时也不必独占，
        否则用户会用音量键去碰一段根本没有遮罩可点的空窗。
        """
        self.volume_stop.set_active(self.isVisible())

    def _show_mask(self):
        """覆盖重定向窗口不走 showFullScreen，直接铺满整块屏幕"""
        scr = self.screen() or QApplication.primaryScreen()
        if scr:
            self.setGeometry(scr.geometry())
        # 显示之后才真正套用「不接输入」：窗口标志要在原生窗口存在时才生效
        self.show()
        self.raise_()
        self._sync_volume_grab()

    def hideEvent(self, ev):
        # 所有隐藏路径（后端点完、穿透、截图抬高、最短显示到期）都会经过这里，
        # 统一在这里松手，音量键一消失就恢复
        self._sync_volume_grab()
        super().hideEvent(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._sync_volume_grab()

    def _tick(self):
        if not self.isVisible():
            return
        self.phase = (self.phase + 1) % 60
        # 文字保持静态，不跟着走点；动画交给 logo（见 _sway）
        self._sway()
        self.update()

    def _sway(self):
        """logo 绕自身可见中心左右摆动，并轻微上下浮动

        做法是「旋转 + 平移」而不是改文字/贴图，代价极低：
        - 绕中心左右各摆 SWAY_DEG 度，用正弦做缓入缓出，端点不突兀
        - 摆到两侧时略微下沉浮起，看起来像悬浮着荡动
        动画只在遮罩可见时跑；隐藏/让开时计时器照常走但不动窗口，省开销。
        """
        if self.logo.pixmap().isNull():
            return
        # 30fps 下一帧 0.09 rad，左右一个完整来回约 2.3 秒，接近轻微的呼吸节奏；
        # 太快会显得焦躁，太慢像没动
        self._sway_t += 0.09
        ang = SWAY_DEG * math.sin(self._sway_t)
        dy = SWAY_RISE * math.sin(self._sway_t * 2.0)
        self.logo.set_sway(ang, dy)

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


_ROOT = str(Path(__file__).resolve().parent.parent)


def server_name() -> str:
    """单实例套接字名，带上安装目录指纹：不同安装位置互不影响"""
    return "DeckMeidoOverlay-" + hashlib.sha1(
        _ROOT.encode("utf-8")
    ).hexdigest()[:12]


def _alive(pid: int) -> bool:
    """进程是否真的还活着（僵尸进程算死）"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            st = f.read().rsplit(b")", 1)[-1].split()
        if st and st[0] == b"Z":
            return False
    except Exception:
        pass
    return True


def _overlay_pids() -> list[int]:
    """扫出本安装目录下所有 overlay.py 进程（不含自己）

    套接字只能问到一个实例，被抢走套接字的「聋子孤儿」只能靠扫 /proc 找出来，
    否则它会一直留在桌面上画遮罩。
    """
    me = os.getpid()
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except Exception:
        return found
    for e in entries:
        if not e.isdigit():
            continue
        pid = int(e)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\0") if a]
        except Exception:
            continue
        if not any(a.endswith("overlay.py") for a in args):
            continue
        # 只认本安装目录的遮罩：命令行或工作目录里得有自己的路径
        ok = _ROOT in " ".join(args)
        if not ok:
            try:
                ok = os.readlink(f"/proc/{pid}/cwd") in (_ROOT, _ROOT + "/server")
            except Exception:
                ok = False
        if ok:
            found.append(pid)
    return found


def _wait_dead(pids, timeout: float) -> list[int]:
    """等这批进程真正消失，返回超时后仍活着的"""
    alive = [p for p in pids if _alive(p)]
    end = time.time() + timeout
    while alive and time.time() < end:
        time.sleep(0.05)
        alive = [p for p in alive if _alive(p)]
    return alive


def acquire_single_instance(force: bool = False) -> QLocalServer | None:
    """单实例锁：桌面上只允许一层遮罩。

    遮罩由后端按需拉起，后端重启后旧的遮罩会变成孤儿继续挂着，
    多来几次就叠成好几层。这里做本地套接字查重：
      - 已有实例活着：非 force 时直接退出；force 时先让它自己关掉，再接管
      - 连不上（进程已崩）：清掉残留套接字文件，重新监听

    force 接管必须「确认对方死透」才能 listen。旧实现只盲等 2 秒就抢套接字，
    对方超过 2 秒才退出时套接字已被抢走，它就变成一个收不到 quit、
    却仍在画遮罩的聋子孤儿——正是重复遮罩的根源。

    名字带上安装目录指纹，不同安装位置互不影响。
    """
    name = server_name()

    def request(cmd: bytes) -> bytes:
        """向已有实例发一条命令，返回对方的回应；连不上返回 b""。

        对端可能在收完命令后立刻断开，此时 state 已不是 Connected，
        再调 waitForBytesWritten 会刷 "not allowed in UnconnectedState" 警告。
        """
        sock = QLocalSocket()
        sock.connectToServer(name)
        if not sock.waitForConnected(300):
            return b""
        reply = b""
        try:
            sock.write(cmd)
            # 只 flush 不等 bytesWritten：本地套接字上这几字节会立刻进内核缓冲，
            # 而对端收完就断开时再调 waitForBytesWritten 会刷
            # "not allowed in UnconnectedState" 警告（连 try/except 也拦不住，它是 Qt 内部打印）
            sock.flush()
            if sock.waitForReadyRead(500):
                reply = bytes(sock.readAll())
        except Exception:
            pass
        finally:
            try:
                sock.disconnectFromServer()
            except Exception:
                pass
        return reply

    def parse_pid(reply: bytes) -> int | None:
        # 回应格式 b"pid:1234"
        if not reply.startswith(b"pid:"):
            return None
        try:
            return int(reply[4:].strip())
        except ValueError:
            return None

    replied = bool(request(b"ping"))
    others = _overlay_pids()          # 含拿不到套接字的聋子孤儿
    if (replied or others) and not force:
        return None

    if force and (replied or others):
        # 先礼：请它自己退位（新版会回自己的 pid）
        targets = set(others)
        pid = parse_pid(request(b"quit"))
        if pid:
            targets.add(pid)
        leftover = _wait_dead(targets, 3.0)
        if leftover:
            # 后兵：真死了不退出就强杀，绝不允许留给接任者一个聋子孤儿
            for p in leftover:
                try:
                    os.kill(p, signal.SIGKILL)
                except Exception:
                    pass
            _wait_dead(leftover, 2.0)

    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        # 撞车：另一种可能是另一个实例刚好抢先，让它去
        if request(b"ping"):
            return None
        print(f"[DeckMeidoOverlay] 单实例监听失败：{server.errorString()}",
              file=sys.stderr)
    return server


def main() -> int:
    force = "--force" in sys.argv[1:]
    app = QApplication([])
    app.setApplicationName("DeckMeidoOverlay")
    app.setFont(ui_font(13))

    single = acquire_single_instance(force=force)
    if single is None:
        return 0

    mask = Mask()
    mask.hide()

    def shutdown() -> None:
        """退出前主动让出套接字。

        不这么做的话，接任者只能盲等一段时间后强删套接字文件；
        万一我还没退干净，套接字已被它接管，我就成了一个收不到 quit、
        却仍在桌面上画遮罩的聋子孤儿——正是重复遮罩的根源。
        """
        try:
            single.close()
            QLocalServer.removeServer(server_name())
        except Exception:
            pass
        # 退出前主动松开音量键独占，别让它停在「按了没反应」的状态
        try:
            mask.volume_stop.shutdown()
        except Exception:
            pass
        app.quit()

    def on_connection() -> None:
        while single.hasPendingConnections():
            conn = single.nextPendingConnection()
            if conn is None:
                break
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable() == 0:
                conn.waitForReadyRead(200)
            payload = bytes(conn.readAll()).strip()
            if payload == b"quit":
                # 后端拉起了新版：先把 pid 报出去，让它确认我死透了再接管
                try:
                    conn.write(b"pid:" + str(os.getpid()).encode())
                    conn.flush()
                except Exception:
                    pass
                # 交回事件循环再退，别在槽函数里直接 quit
                QTimer.singleShot(0, shutdown)
                return
            # ping：回一句证明我还活着（force 接管方据此判断要不要接管）
            if payload == b"ping":
                try:
                    conn.write(b"pong")
                    conn.flush()
                except Exception:
                    pass
            conn.disconnectFromServer()

    single.newConnection.connect(on_connection)

    try:
        app.exec()
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
