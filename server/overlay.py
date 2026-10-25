"""DeckMeido 操作遮罩

agent 操作这台设备期间，在桌面上叠加一层半透明全屏遮罩，显示当前正在做什么。
用户点一下遮罩（触摸屏轻触、鼠标点击、触摸板点击均可）立刻调用 /api/cancel
终止当前操作并撤掉遮罩。

遮罩不读取任何输入设备，也不需要任何系统权限。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QLabel, QVBoxLayout, QWidget,
)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_PX = 152     # 卡片上方 logo 的显示边长
LOGO_GAP = 14     # logo 图形与卡片之间的实际间距（按可见图形算，不含透明留白）
LOGO_DY = 10      # logo+卡片的整体在垂直居中基础上额外下移的像素

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

        # logo 独立于卡片，浮在卡片正上方（进卡片会撑高卡片、挤乱排版）
        self.logo = QLabel(self)
        self._logo_inset_top = 0
        self._logo_inset_bottom = 0
        self.logo.setPixmap(self._load_logo())
        self.logo.setFixedSize(LOGO_PX, LOGO_PX)
        self.logo.setAlignment(Qt.AlignCenter)
        self.logo.setStyleSheet("background: transparent;")
        self.logo.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        lay.addWidget(self.title)
        lay.addWidget(self.action, 0, Qt.AlignHCenter)
        lay.addSpacing(4)
        lay.addWidget(self.hint, 0, Qt.AlignHCenter)

        self._shown_at = 0.0
        self._cancelled = False
        self._passthrough = False
        self._input_transparent = False
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

    def _load_logo(self) -> QPixmap:
        """加载 assets/logo.png；缺失时留空 pixmap，遮罩照常工作。

        logo 图四周带透明留白（原图 512 里图形只占 y≈90~381，下方约 1/4 是空的），
        排版若按整张 pixmap 算间距，眼睛看到的空档会比参数值大得多。
        这里顺便量出可见图形的上下留白，_relayout 只按可见部分占位。"""
        self._logo_inset_top = 0
        self._logo_inset_bottom = 0
        path = ASSETS_DIR / "logo.png"
        pm = QPixmap(str(path)) if path.exists() else QPixmap()
        if pm.isNull():
            return pm
        pm = pm.scaled(
            LOGO_PX, LOGO_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._logo_inset_top, self._logo_inset_bottom = self._visual_insets(pm)
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
        # 穿透期间让整个窗口不接输入（WindowTransparentForInput）：不只是「自己忽略
        # 事件」，而是让系统把这层从命中测试里摘掉，点击直接落到下面的窗口。
        # 以前只用 WA_TransparentForMouseEvents，遮罩窗口本身仍参与命中测试，
        # agent 那一下点击的坐标只要落在遮罩上，就会被当成「用户终止」。
        # 注意窗口不能藏：画面仍要显示「正在操作」，用户也还能看到进度
        self.setAttribute(Qt.WA_TransparentForMouseEvents, on)
        self._apply_input_transparency(on)
        if not on and self._show_wanted and not self._lifted and not self.isVisible():
            self._show_mask()
        self.repaint()

    def _apply_input_transparency(self, on: bool):
        """按需切换「整个窗口不接输入」的原生标志

        只在遮罩已显示时动标志：X11 下改窗口标志会重建原生窗口，
        没显示时改了没意义，等 _show_mask 显示出来再套用。
        """
        want = on and self.isVisible()
        if want == self._input_transparent:
            return
        self._input_transparent = want
        flags = self.windowFlags()
        if want:
            flags |= Qt.WindowTransparentForInput
        else:
            flags &= ~Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        # setWindowFlags 会把窗口藏起来，必须立刻重新显示并复位几何，否则遮罩会消失
        if self.isVisible():
            scr = self.screen() or QApplication.primaryScreen()
            if scr:
                self.setGeometry(scr.geometry())
            self.show()
            self.raise_()


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
        （QFrame 放在顶层窗口里不会被自动布局管理）。

        logo 与卡片作为一个整体垂直居中。间距按 logo 的「可见图形」算：
        logo 图片自带透明留白，若按整张 pixmap 占位，参数 14px 会被撑成 50+px，
        看着就像两块东西离得很远。"""
        self.card.setFixedWidth(min(620, self.width() - 60))
        self.card.layout().activate()
        h = self.card.layout().sizeHint().height()
        self.card.setFixedHeight(h)

        has_logo = not self.logo.pixmap().isNull()
        # 可见图形在 pixmap 内的高度：整图减去透明留白
        vis_h = LOGO_PX - self._logo_inset_top - self._logo_inset_bottom
        gap = LOGO_GAP if has_logo else 0
        total = (vis_h + gap if has_logo else 0) + h
        top = (self.height() - total) // 2 + LOGO_DY

        if has_logo:
            self.logo.show()
            # 让 pixmap 内的可见图形顶端正好落在 top，透明留白往上溢出即可
            self.logo.move((self.width() - LOGO_PX) // 2, top - self._logo_inset_top)
            top += vis_h + gap
        else:
            self.logo.hide()

        self.card.move((self.width() - self.card.width()) // 2, top)

    # ---------- 绘制 ----------
    def _show_mask(self):
        """覆盖重定向窗口不走 showFullScreen，直接铺满整块屏幕"""
        scr = self.screen() or QApplication.primaryScreen()
        if scr:
            self.setGeometry(scr.geometry())
        # 显示之后才真正套用「不接输入」：窗口标志要在原生窗口存在时才生效
        if self._passthrough:
            self._apply_input_transparency(True)
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
