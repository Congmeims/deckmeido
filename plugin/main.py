"""DeckMeido 的 Decky 插件后端。

插件以 root 身份加载（见 plugin.json 的 flags），只做三件事：
1. 从 gamescope 的 PipeWire 节点抓一帧当前游戏画面
2. 转发 QAM 和本机 :18765 之间的对话（包括流式增量）
3. 告诉 QAM 自己能不能用（后端在不在、token 读不读得到、画面能不能抓）

推理逻辑一行都不在这里：全部复用 DeckMeido 已有的后端，
插件只是"截图 + 转发 + 显示"。

前端不直连后端，而是调这里的 start_turn/wait_final/cancel，流式内容通过
`deckmeido_delta` 事件回推。这样 QAM 侧不用管 CEF 的 CSP 会不会拦 ws。
"""

import asyncio
import base64
import itertools
import json
import os
import shutil
import tomllib
from pathlib import Path

import decky

# websockets 不在 Decky 自带的 python 环境里，但前端不直连后端就必须靠它转发。
# 装了就用它，没装则退到标准库的原始握手实现（见 _RawWSClient），功能一致。
try:
    from websockets.asyncio.client import connect as _ws_connect
except ImportError:
    try:
        from websockets import connect as _ws_connect
    except ImportError:
        _ws_connect = None


def _find_install_dir() -> Path:
    """找 DeckMeido 的安装目录（里面要有 server/config.toml）。

    插件自己装在 ~/homebrew/plugins/DeckMeido，和后端不是同一个目录，
    所以不能靠 __file__ 往上推，只能按已知位置挨个试。
    """
    homes = ["/home/deck"]
    try:
        # root 下 Path.home() 是 /root，这里枚举 home 目录下的真实用户
        homes += [str(p) for p in Path("/home").iterdir() if p.is_dir()]
    except Exception:
        pass
    for home in dict.fromkeys(homes):
        for name in ("deckmeido", "DeckMeido", "SDagent", "sdagent"):
            cand = Path(home) / name
            if (cand / "server" / "config.toml").exists():
                return cand
    return Path("/home/deck/deckmeido")


INSTALL_DIR = _find_install_dir()
CONFIG_PATH = INSTALL_DIR / "server" / "config.toml"


def _load_server_cfg() -> dict:
    """读后端的 [server] 段，拿 port 和 token"""
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("server") or {}
    except Exception:
        return {}


# ---------- 身份与运行目录 ----------
#
# 游戏模式下抓帧必须站在 gamescope 所属的普通用户那边做（root 连不上它的
# PipeWire socket），所以这里的命令一律以 deck 用户身份跑。

def _target_uid() -> int:
    """要降权到哪个 uid：优先 /home/deck 的属主，退到 1000"""
    try:
        return Path("/home/deck").stat().st_uid
    except Exception:
        return 1000


_RUN_AS: list[str] | None = None


def _run_as() -> list[str]:
    """以 deck 用户跑命令的前缀；自己已经是 deck 就是空列表"""
    global _RUN_AS
    if _RUN_AS is not None:
        return _RUN_AS
    if os.getuid() == _target_uid():
        _RUN_AS = []
        return _RUN_AS
    if os.getuid() != 0:
        # 既不是 root 也不是 deck（比如开发机），降权无从谈起
        _RUN_AS = []
        return _RUN_AS
    user = "deck"
    if shutil.which("runuser"):
        _RUN_AS = ["runuser", "-u", user, "--"]
    elif shutil.which("sudo"):
        _RUN_AS = ["sudo", "-u", user, "--"]
    else:
        decky.logger.warning("既没有 runuser 也没有 sudo，只能以当前身份抓帧")
        _RUN_AS = []
    return _RUN_AS


def _runtime_dir() -> str:
    """gamescope 的 PipeWire socket 在 /run/user/<deck uid> 下"""
    return f"/run/user/{_target_uid()}"


async def _gamescope_pw_node() -> tuple[str | None, str]:
    """找 gamescope 的 PipeWire 视频源节点 id，第二个返回值是失败原因"""
    if not shutil.which("pw-dump"):
        return None, "机器上没有 pw-dump（pipewire 工具未安装）"
    cmd = _run_as() + ["pw-dump"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": _runtime_dir()},
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        return None, "pw-dump 超时（15s）"
    except Exception as exc:
        return None, f"pw-dump 跑不起来：{exc}"
    try:
        nodes = json.loads(out or b"[]")
    except Exception:
        detail = (err or b"").decode("utf-8", "replace").strip()[-200:]
        return None, f"pw-dump 输出解析失败{('：' + detail) if detail else ''}"
    for o in nodes:
        props = (o.get("info") or {}).get("props") or {}
        if (
            props.get("node.name") == "gamescope"
            and props.get("media.class") == "Video/Source"
        ):
            return str(o.get("id")), ""
    return None, "没找到 gamescope 的画面节点：可能不在游戏模式"


_GST_SEARCH = ("/usr/bin", "/usr/local/bin")


def _find_gst() -> str | None:
    """root 的 PATH 有时不含 /usr/bin，所以 PATH 找不到就按已知位置再找一遍"""
    found = shutil.which("gst-launch-1.0")
    if found:
        return found
    for d in _GST_SEARCH:
        cand = Path(d) / "gst-launch-1.0"
        if cand.exists():
            return str(cand)
    return None


async def _capture_once(node: str, gst: str, timeout: int = 25) -> tuple[bool, str]:
    """抓一帧，返回 (成功, base64 或错误说明)"""
    tmp = Path("/tmp") / f"deckmeido_shot_{os.getpid()}_{next(_SHOT_SEQ)}.png"
    cmd = _run_as() + [
        gst, "-q", f"pipewiresrc path={node} num-buffers=6", "!",
        "videoconvert", "!", "pngenc", "!", f"filesink location={tmp}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": _runtime_dir()},
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if not tmp.exists() or tmp.stat().st_size < 10000:
            detail = (err or b"").decode("utf-8", "replace").strip()[-300:]
            return False, f"抓帧没拿到有效画面{('：' + detail) if detail else ''}"
        return True, base64.b64encode(tmp.read_bytes()).decode("ascii")
    except asyncio.TimeoutError:
        return False, f"抓帧超时（{timeout}s）"
    except Exception as exc:
        return False, f"抓帧出错：{exc}"
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


_SHOT_SEQ = itertools.count()
_SHOT_LOCK = asyncio.Lock()


async def grab_screenshot() -> tuple[bool, str]:
    """抓一帧游戏画面，返回 (成功, base64 或错误说明)

    写法和 server/tools.py 的 _screenshot_gamescope 保持一致：连抓 6 帧覆盖
    同一个文件，拿到的是最后一帧（避免拿到刚启动时的黑帧）。
    节点找不到或第一次抓失败时，给一次短等待再试 —— 刚进游戏的那几秒
    gamescope 的节点常常还没稳定。
    """
    node, why = await _gamescope_pw_node()
    if not node:
        return False, why

    gst = _find_gst()
    if not gst:
        return False, "机器上没有 gst-launch-1.0（gst-plugins-base 未安装）"

    # 同一时刻只允许一个抓帧进程，否则前端连点会把 /tmp 搅乱
    async with _SHOT_LOCK:
        ok, payload = await _capture_once(node, gst)
        if not ok:
            await asyncio.sleep(1.0)
            ok, payload = await _capture_once(node, gst, timeout=20)
    return ok, payload


# ---------- 虚拟手柄（uinput） ----------
#
# gamescope 既不认 XTest 也不认 Wayland 的虚拟键盘协议，能进游戏模式的只有
# /dev/uinput 虚拟设备（Steam Input、Sunshine 走的都是这条）。插件以 root
# 加载，直接写 /dev/uinput 就行，不需要 libei 或任何外部二进制。
#
# 为什么不能只建键盘：摇杆（ABS_X/ABS_Y）和扳机（ABS_Z/ABS_RZ）是绝对值轴，
# 一个 EV_KEY-only 的设备扛不了；反过来手柄设备也发不出键盘字符。所以这里是
# 一个带 EV_KEY + EV_ABS 的手柄设备，只用于「模拟手柄」，键盘仍由后端 ydotool 负责。

import struct

_UI_DEV_CREATE = 0x5501
_UI_DEV_DESTROY = 0x5502
_UI_SET_EVBIT = 0x40045564
_UI_SET_KEYBIT = 0x40045565
_UI_SET_ABSBIT = 0x40045567
_UI_DEV_SETUP = 0x405C5503
_UI_ABS_SETUP = 0x401C5504
_UI_GET_VERSION = 0x80045505

_EV_SYN, _EV_KEY, _EV_ABS = 0x00, 0x01, 0x03
_ABS_X, _ABS_Y, _ABS_Z, _ABS_RZ = 0x00, 0x01, 0x02, 0x05
_BTN_SOUTH, _BTN_EAST, _BTN_NORTH, _BTN_WEST = 0x130, 0x131, 0x133, 0x134
_BTN_TL, _BTN_TR = 0x136, 0x137

# 摇杆量程按 Linux 手柄惯例走 -32767..32767，死区留 0
_STICK_MIN, _STICK_MAX = -32767, 32767
# 扳机按 Steam 习惯走 0..32767
_TRIG_MAX = 32767

# 面板上能按的名字 → 键位。名字取玩家熟悉的叫法，ABXY 按 Xbox 布局对应。
_GAMEPAD_KEYS = {
    "a": _BTN_SOUTH,
    "b": _BTN_EAST,
    "x": _BTN_NORTH,
    "y": _BTN_WEST,
    "lb": _BTN_TL,
    "rb": _BTN_TR,
    # 方向键在 Linux 里也是 BTN_* 键位
    "up": 0x220,
    "down": 0x221,
    "left": 0x222,
    "right": 0x223,
    "start": 0x13B,
    "select": 0x13A,
}


class _Gamepad:
    """一个常驻的虚拟手柄，多个按钮共用一个设备。

    设备只在第一次按键时创建，闲置 _IDLE_CLOSE 秒后销毁 —— 一直挂着一个
    虚拟手柄会让 Steam 认为有手柄连着（比如强制把界面切成手柄导航）。
    """

    _IDLE_CLOSE = 60.0

    def __init__(self):
        self._fd: int | None = None
        self._lock = asyncio.Lock()
        self._idle: asyncio.Task | None = None

    @property
    def open(self) -> bool:
        return self._fd is not None

    def _emit(self, typ: int, code: int, value: int):
        """struct input_event：timeval 的 tv_sec/tv_usec 都是 64 位 long，
        所以这里写 qq 而不是 ll（32 位系统上 ll 只有 4 字节，结构体会对不上）"""
        os.write(self._fd, struct.pack("qqHHi", 0, 0, typ, code, value))

    def _syn(self):
        self._emit(_EV_SYN, 0, 0)

    def _create(self):
        import fcntl

        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            # 键位表要先问内核要版本，旧内核不认 UI_DEV_SETUP（这时只能靠
            # 老的 uinput_user_dev 写结构体，SteamOS 不会出现，直接报错）
            try:
                fcntl.ioctl(fd, _UI_GET_VERSION, b"\x00" * 4)
            except OSError:
                pass
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_SYN)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_ABS)
            for code in _GAMEPAD_KEYS.values():
                fcntl.ioctl(fd, _UI_SET_KEYBIT, code)
            for code, lo, hi in (
                (_ABS_X, _STICK_MIN, _STICK_MAX),
                (_ABS_Y, _STICK_MIN, _STICK_MAX),
                (_ABS_Z, 0, _TRIG_MAX),
                (_ABS_RZ, 0, _TRIG_MAX),
            ):
                fcntl.ioctl(fd, _UI_SET_ABSBIT, code)
                # uinput_abs_setup：code(2) + pad(2) + absinfo{value,min,max,fuzz,flat,resolution}(6*4)
                fcntl.ioctl(
                    fd, _UI_ABS_SETUP,
                    struct.pack("HHiiiiii", code, 0, 0, lo, hi, 0, 0, 0),
                )
            # uinput_setup：id{bustype,vendor,product,version}(4*2) + name(80) + ff_effects_max(4)
            fcntl.ioctl(
                fd, _UI_DEV_SETUP,
                struct.pack("HHHH80sI", 0x03, 0x045E, 0x02FD, 0x0001, b"DeckMeido Gamepad", 0),
            )
            fcntl.ioctl(fd, _UI_DEV_CREATE)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        decky.logger.info("虚拟手柄已创建")
        # 设备刚出现时内核还没有它的节点，先给一小段时间让游戏侧枚举到
        self._syn()

    def _destroy(self):
        if self._fd is None:
            return
        try:
            import fcntl

            fcntl.ioctl(self._fd, _UI_DEV_DESTROY)
        except Exception:
            pass
        try:
            os.close(self._fd)
        except Exception:
            pass
        self._fd = None
        decky.logger.info("虚拟手柄已销毁")

    def _touch_idle(self):
        """空闲计时：一段时间没人按就把设备收掉"""
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.create_task(self._close_later())

    async def _close_later(self):
        try:
            await asyncio.sleep(self._IDLE_CLOSE)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._fd is not None:
                self._destroy()

    async def press(self, key: str, ms: int = 80) -> dict:
        """按一下再松开。ms 是按住时长，太短游戏会丢（一帧里按下又抬起）"""
        name = str(key or "").strip().lower()
        code = _GAMEPAD_KEYS.get(name)
        if code is None:
            return {"ok": False, "error": f"不认识的手柄键：{key}"}
        ms = max(30, min(int(ms or 80), 2000))
        async with self._lock:
            try:
                if self._fd is None:
                    self._create()
                self._emit(_EV_KEY, code, 1)
                self._syn()
                await asyncio.sleep(ms / 1000)
                self._emit(_EV_KEY, code, 0)
                self._syn()
            except FileNotFoundError:
                return {"ok": False, "error": "没有 /dev/uinput：uinput 内核模块未加载"}
            except PermissionError:
                return {"ok": False, "error": "打不开 /dev/uinput：插件需要以 root 加载"}
            except Exception as exc:
                return {"ok": False, "error": f"手柄注入失败：{exc}"}
            self._touch_idle()
        return {"ok": True, "key": name, "ms": ms}

    async def stick(self, x: float, y: float) -> dict:
        """左摇杆，两个参数都是 -1..1（1 是右/下）"""
        try:
            vx = int(max(-1.0, min(1.0, float(x))) * _STICK_MAX)
            vy = int(max(-1.0, min(1.0, float(y))) * _STICK_MAX)
        except (TypeError, ValueError):
            return {"ok": False, "error": "摇杆值要是 -1..1 的数字"}
        async with self._lock:
            try:
                if self._fd is None:
                    self._create()
                self._emit(_EV_ABS, _ABS_X, vx)
                self._emit(_EV_ABS, _ABS_Y, vy)
                self._syn()
            except FileNotFoundError:
                return {"ok": False, "error": "没有 /dev/uinput：uinput 内核模块未加载"}
            except PermissionError:
                return {"ok": False, "error": "打不开 /dev/uinput：插件需要以 root 加载"}
            except Exception as exc:
                return {"ok": False, "error": f"摇杆注入失败：{exc}"}
            self._touch_idle()
        return {"ok": True, "x": vx, "y": vy}

    async def close(self):
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        async with self._lock:
            self._destroy()


_GAMEPAD = _Gamepad()


# ---------- 与后端 :18765 的通话 ----------
#
# 后端每轮对话的广播是发给「所有」ws 连接的，同一个后端可能同时挂着网页端。
# 所以每次开工先开一条常驻连接并拿到 history 里的 sid，转发时只认这个 sid。

class _RawWSClient:
    """不依赖 websockets 库的最小客户端（只要文本帧，够后端用）"""

    def __init__(self, url: str):
        self.url = url
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf = b""

    async def __aenter__(self):
        from urllib.parse import urlsplit, urlunsplit

        u = urlsplit(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        target = urlunsplit(("", "", u.path or "/", u.query, ""))
        self._reader, self._writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://{host}:{port}\r\n"
            "\r\n"
        )
        self._writer.write(req.encode())
        await self._writer.drain()
        head = await self._reader.readuntil(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError(f"握手被拒：{status_line[:80]!r}")
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        w, self._writer = self._writer, None
        if w:
            try:
                w.write(_frame(0x8, b""))
                await w.drain()
            except Exception:
                pass
            w.close()

    async def send(self, text: str):
        assert self._writer
        self._writer.write(_frame(0x1, text.encode()))
        await self._writer.drain()

    async def recv(self) -> str:
        while True:
            frame = await self._read_frame()
            if frame is None:
                raise RuntimeError("后端关掉了连接")
            opcode, payload = frame
            if opcode == 0x1:
                return payload.decode()
            if opcode == 0x8:
                raise RuntimeError("后端发来 close 帧")

    async def _filled(self, n: int):
        assert self._reader
        while len(self._buf) < n:
            chunk = await self._reader.read(65536)
            if not chunk:
                return None
            self._buf += chunk
        return self._buf

    async def _read_frame(self) -> tuple[int, bytes] | None:
        """返回 (opcode, payload)；服务端帧不加掩码，分片帧拼起来"""
        buf_parts: list[bytes] = []
        first_op: int | None = None
        while True:
            head = await self._filled(2)
            if head is None:
                return None
            b0, b1 = head[0], head[1]
            opcode = b0 & 0x0F
            fin = bool(b0 & 0x80)
            length = b1 & 0x7F
            masked = bool(b1 & 0x80)
            offset = 2
            if length == 126:
                ext = await self._filled(offset + 2)
                if ext is None:
                    return None
                length = int.from_bytes(ext[2:4], "big")
                offset += 2
            elif length == 127:
                ext = await self._filled(offset + 8)
                if ext is None:
                    return None
                length = int.from_bytes(ext[2:10], "big")
                offset += 8
            mask = b""
            if masked:
                ext = await self._filled(offset + 4)
                if ext is None:
                    return None
                mask = ext[offset:offset + 4]
                offset += 4
            body = await self._filled(offset + length)
            if body is None:
                return None
            payload = body[offset:offset + length]
            self._buf = body[offset + length:]
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x9:  # ping
                assert self._writer
                self._writer.write(_frame(0xA, payload))
                await self._writer.drain()
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                first_op = opcode
                buf_parts.append(payload)
                if fin:
                    return first_op, b"".join(buf_parts)
            elif opcode == 0x0:
                buf_parts.append(payload)
                if fin:
                    return first_op or 0x1, b"".join(buf_parts)
            elif opcode == 0x8:
                return 0x8, payload


def _frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    """客户端发出的帧必须加掩码"""
    head = bytearray([0x80 | opcode])
    n = len(payload)
    mask_bit = 0x80 if mask else 0
    if n < 126:
        head.append(mask_bit | n)
    elif n < 65536:
        head.append(mask_bit | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(mask_bit | 127)
        head += n.to_bytes(8, "big")
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + bytes(c ^ key[i % 4] for i, c in enumerate(payload))


class _Backend:
    """一条连到后端的常驻 ws，用来转发对话"""

    def __init__(self, url: str):
        self.url = url
        self._impl = None
        self._send_lock = asyncio.Lock()
        self.sid = ""

    async def open(self):
        if _ws_connect is not None:
            self._impl = await _ws_connect(
                self.url, max_size=32 * 1024 * 1024, open_timeout=10
            )
        else:
            self._impl = await _RawWSClient(self.url).__aenter__()

    async def recv(self) -> str:
        msg = await self._impl.recv()
        return msg if isinstance(msg, str) else msg.decode()

    async def send(self, payload: dict):
        text = json.dumps(payload)
        async with self._send_lock:
            await self._impl.send(text)

    async def close(self):
        impl, self._impl = self._impl, None
        if not impl:
            return
        try:
            await impl.close()
        except Exception:
            pass


_BACKEND: _Backend | None = None
_READER: asyncio.Task | None = None
_ACC = ""            # 本轮已收到的增量正文
_LAST_REPLY = ""     # 本轮确定下来的完整正文
_FINAL_EVENT = asyncio.Event()


def _backend_url(port: int, token: str) -> str:
    return f"ws://127.0.0.1:{port}/ws?token={token}"


def _emit(msg: dict):
    """把一条消息推给 QAM（前端 addEventListener("deckmeido_delta")）"""
    try:
        decky.emit("deckmeido_delta", msg)
    except Exception as exc:
        decky.logger.warning(f"推送前端失败：{exc}")


def _finalize(cancelled: bool = False):
    """把这一轮冻结下来并推给前端。

    后端的收尾顺序是 delta(final=True, reply) → assistant_message(全文) → status；
    所以真正能被当成「这轮结束」的只有前两个，其中 assistant_message 是唯一
    带完整正文的（没有正文的一轮后端压根不发）。status.running 不能当收尾依据：
    current_task 在回复广播之前就被置空，那一刻正文还没到。
    """
    global _LAST_REPLY
    _LAST_REPLY = _ACC
    _emit({"kind": "final", "text": _ACC, "cancelled": cancelled})
    _FINAL_EVENT.set()


async def _read_loop(bk: _Backend):
    """常驻读：history 里拿 sid，之后只转发本轮的消息"""
    global _ACC
    while True:
        try:
            raw = await bk.recv()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decky.logger.info(f"后端连接断开：{exc}")
            return
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        typ = msg.get("type")
        if typ == "history" and not bk.sid:
            bk.sid = str(msg.get("sid") or "")
            continue
        if typ == "status":
            # 状态广播不带 sid（它在改配置时也会发），所以放在 sid 过滤之前
            _emit({"kind": "status", "running": bool((msg.get("data") or {}).get("running"))})
            continue
        if not bk.sid or msg.get("sid") != bk.sid:
            # 别的会话（网页端）的消息，与 QAM 这一页无关
            continue
        if typ == "delta":
            text = str(msg.get("text") or "")
            if text:
                _ACC += text
                _emit({"kind": "delta", "text": text})
            if msg.get("final"):
                # 正文已完整，但先不冻结：后面还有一条 assistant_message 要覆盖它
                _ACC = str(msg.get("reply") or _ACC)
        elif typ == "assistant_message":
            if msg.get("cancelled"):
                # 用户点了终止：已生成的部分比「已终止当前操作」有用，保留它
                _ACC = _ACC or str(msg.get("text") or "已终止当前操作")
                _finalize(cancelled=True)
            else:
                # 唯一带完整正文的收尾消息
                _ACC = str(msg.get("text") or _ACC)
                _finalize()
        elif typ == "tool_activity":
            # 「正在读文件：…」这类过程提示，只给界面看，不进正文
            _emit({"kind": "tool", "text": str(msg.get("text") or "")})



async def _ensure_backend() -> tuple[_Backend | None, str]:
    """拿到可用的后端连接，第二个返回值是错误说明"""
    global _BACKEND, _READER
    cfg = _load_server_cfg()
    if not CONFIG_PATH.exists():
        return None, f"找不到配置：{CONFIG_PATH}"
    port = int(cfg.get("port", 18765))
    token = str(cfg.get("token", "") or "").strip().lower()
    if not token:
        return None, "后端配置里的 token 是空的，先在网页设置页生成一个"
    if _BACKEND and _READER and not _READER.done():
        return _BACKEND, ""
    await _drop_backend()
    bk = _Backend(_backend_url(port, token))
    try:
        await bk.open()
    except Exception as exc:
        return None, f"连不上后端 :{port}（{exc}）"
    _BACKEND = bk
    _READER = asyncio.create_task(_read_loop(bk))
    # 等 history：拿不到 sid 就无法过滤别的客户端的广播
    for _ in range(100):
        if bk.sid:
            break
        await asyncio.sleep(0.05)
    return bk, ""


async def _drop_backend():
    global _BACKEND, _READER
    r, _READER = _READER, None
    bk, _BACKEND = _BACKEND, None
    if r:
        r.cancel()
        try:
            await r
        except BaseException:
            pass
    if bk:
        await bk.close()


class Plugin:
    async def get_status(self) -> dict:
        """QAM 打开时先问一次：后端连得上吗、截图能不能用"""
        cfg = _load_server_cfg()
        port = cfg.get("port", 18765)
        node, why = await _gamescope_pw_node()
        return {
            "install_dir": str(INSTALL_DIR),
            "config_ok": CONFIG_PATH.exists(),
            "port": port,
            # token 只在插件后端内部拼 URL 用，不再交给前端
            "token_ok": bool(str(cfg.get("token", "") or "").strip()),
            "gamescope": node is not None,
            "capture_ok": node is not None and _find_gst() is not None,
            "capture_reason": why,
            "uid": os.getuid(),
            "as_user": " ".join(_run_as()) or "(self)",
            "proxy": "websockets" if _ws_connect else "raw",
            # 虚拟手柄能不能用：设备节点在 + 进程有写权限
            "gamepad": os.access("/dev/uinput", os.W_OK) if os.path.exists("/dev/uinput") else False,
        }

    async def screenshot(self) -> dict:
        ok, payload = await grab_screenshot()
        if ok:
            return {"ok": True, "image": payload}
        return {"ok": False, "error": payload}

    async def gamepad_press(self, key: str, ms: int = 80) -> dict:
        """模拟按一下手柄键：a/b/x/y/lb/rb/up/down/left/right/start/select"""
        return await _GAMEPAD.press(key, ms)

    async def gamepad_stick(self, x: float, y: float) -> dict:
        """左摇杆推到 (x, y)，范围 -1..1；推完不会自动回中，
        面板松手时会再调一次 gamepad_stick(0, 0)"""
        return await _GAMEPAD.stick(x, y)

    async def gamepad_close(self) -> dict:
        """收掉虚拟手柄设备（不再需要时才调，避免 Steam 一直以为接了手柄）"""
        await _GAMEPAD.close()
        return {"ok": True}

    async def start_turn(self, text: str, image: str | None = None) -> dict:
        """把一轮对话发给后端。返回后流式内容通过 deckmeido_delta 推给前端"""
        global _ACC, _LAST_REPLY
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "问题是空的"}

        bk, why = await _ensure_backend()
        if not bk:
            return {"ok": False, "error": why}
        _ACC = ""
        _LAST_REPLY = ""
        _FINAL_EVENT.clear()
        payload = {"type": "chat", "text": text, "client": "qam"}
        if image:
            payload["image"] = image
            payload["image_prompt"] = "（这是当前游戏画面，结合画面回答用户的问题）"
        payload["workspace"] = "/home/deck"
        try:
            await bk.send(payload)
        except Exception as exc:
            return {"ok": False, "error": f"发不出去：{exc}"}
        return {"ok": True}

    async def wait_final(self, timeout: float = 900) -> dict:
        """等本轮收尾，返回完整正文。前端收到 final 事件就不用调这个"""
        try:
            await asyncio.wait_for(_FINAL_EVENT.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"等了 {timeout:.0f}s 还没有答复"}
        return {"ok": True, "text": _LAST_REPLY}

    async def cancel(self) -> dict:
        """掐掉这一轮：断开的后端连接会让那边 CancelledError，
        再补一次 /api/cancel，把子进程树一起收掉"""
        global _ACC
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        token = str(cfg.get("token", "") or "")
        await _drop_backend()
        err = ""
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            req = (
                "POST /api/cancel HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"X-Token: {token}\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(req.encode())
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(), timeout=10)
            writer.close()
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                err = resp.split(b"\r\n", 1)[0].decode("utf-8", "replace")[:80]
        except Exception as exc:
            err = str(exc)
        _ACC += "\n（已中断）" if _ACC else "已中断"
        _finalize(cancelled=True)
        if err:
            decky.logger.warning(f"/api/cancel 失败：{err}")
        return {"ok": not err, "error": err}

    async def _main(self):
        decky.logger.info(f"DeckMeido 插件已加载，安装目录 {INSTALL_DIR}")
        _run_as()

    async def _unload(self):
        await _drop_backend()
        # 不显式销毁的话，虚拟手柄会跟着内核一直挂着，下一次加载前都算「有手柄连着」
        await _GAMEPAD.close()
        decky.logger.info("DeckMeido 插件卸载")

    async def _uninstall(self):
        await _drop_backend()
        await _GAMEPAD.close()
        decky.logger.info("DeckMeido 插件卸载")
