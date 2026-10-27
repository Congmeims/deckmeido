"""网页端实时画面（H.264 / fragmented MP4 推流）。

游戏模式复用 gamescope 的 PipeWire 节点：`gst-launch-1.0` 起一条常驻管线
`pipewiresrc ! videoconvert ! videorate ! vaapih264enc ! mp4mux ! fdsink`，
把 MP4 分片字节流直接写进 stdout，本模块按 MP4 box 边界切开后转发给前端，
浏览器用 `<video src="/api/stream">` 边下边播。

为什么是 H.264 而不是 MJPEG：60fps 下 MJPEG 每帧都要整图编码，半个核喂进去
还会发热；H.264 只编帧间差异，vaapi 走 AMD VCN 固定单元，CPU 基本不参与。

桌面模式只能走 portal ScreenCast（首次要在系统对话框里授权），这里不做，
直接回报「当前环境不适用」——宁可明说，也不要让前端挂一张永远不动的画面。

分辨率沿用 gamescope 节点的原始尺寸（掌机 1280×800），不缩放：
由浏览器按容器大小自适应，比在 gst 里再插一层 videoscale 省 CPU。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# 帧率与画质。60fps 是「动作看得顺」的底线（30fps 转身拖影明显）。
# 硬编走 vaapi 时不看 quality，用固定码率 4Mbps：1280×800@60 的 H.264
# 这个档位肉眼无损，局域网也毫无压力；回落软编时按 quality 折算。
DEFAULT_FPS = 60
DEFAULT_QUALITY = 70
VAAPI_KBPS = 4000

# gamescope 在游戏模式下由合成器自己出帧；隔太久没帧说明管线断了
_STALL_SECONDS = 8.0
# 起管线后最多等这么久拿到第一帧（gst 协商 + 编码器初始化需要时间）
_FIRST_FRAME_TIMEOUT = 6.0


def _session_user() -> tuple[int, str] | None:
    """gamescope / PipeWire 所在会话的 (uid, 用户名)

    与 tools 里那份一致：后端常以 root 跑（Decky 插件），此时 os.getuid()
    是 0，而 gamescope 在 deck 用户的图形会话里，套接字在 /run/user/1000。
    """
    import pwd

    if os.getuid() != 0:
        return os.getuid(), pwd.getpwuid(os.getuid()).pw_name
    candidates: list[tuple[int, str]] = []
    try:
        for d in Path("/run/user").iterdir():
            if not d.name.isdigit():
                continue
            uid = int(d.name)
            if uid == 0 or not ((d / "pulse").exists() or (d / "bus").exists()):
                continue
            try:
                candidates.append((uid, pwd.getpwuid(uid).pw_name))
            except KeyError:
                continue
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0] != 1000, x[0]))
    return candidates[0]


async def gamescope_pw_node() -> str | None:
    """游戏模式下 gamescope 的 PipeWire 视频节点 id；找不到返回 None"""
    sess = _session_user()
    env = dict(os.environ)
    if sess:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{sess[0]}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        out, _ = await proc.communicate()
        for o in json.loads(out or b"[]"):
            props = (o.get("info") or {}).get("props") or {}
            if props.get("node.name") == "gamescope" and props.get("media.class") == "Video/Source":
                return str(o.get("id"))
    except Exception:
        return None
    return None


class _Renders:
    """最近一次拿到的画面尺寸（用来在前端角标里显示 1280×800）

    H.264 的宽高在 moov 的 tkhd 里，解析它要跨好几个 box，不如让前端
    播放器把 videoWidth/videoHeight 报上来——播放器本来就已经解出来了。
    """

    size: tuple[int, int] | None = None


_RENDER = _Renders()


def _probe_encoder(env: dict, wrap: list[str]) -> str:
    """探测可用的 H.264 编码器，返回 "vaapi" / "x264" / ""。

    只查插件装没装不够：vaapi 还要设备节点、驱动 profile、以及运行用户对
    /dev/dri/renderD* 的读写权限（后端常以 root 跑、再降权到 deck，root 能开
    不代表 deck 能开）。所以直接按最终运行身份跑一次真实的 init，
    `gst-launch-1.0 -q videotestsrc num-buffers=1 ! vah264enc ! fakesink`
    能返回 0 才算数。
    """
    if not shutil.which("gst-launch-1.0"):
        return ""
    for name, pipeline in (
        ("vaapi", "videotestsrc num-buffers=1 ! videoconvert ! vah264enc ! fakesink"),
        ("x264", "videotestsrc num-buffers=1 ! videoconvert ! x264enc ! fakesink"),
    ):
        try:
            r = subprocess.run([*wrap, "gst-launch-1.0", "-q", *pipeline.split()],
                               capture_output=True, timeout=15, env=env)
        except Exception:
            continue
        if r.returncode == 0:
            return name
    return ""


class Streamer:
    """一条常驻的 gst 推流管线 + 字节流分发。

    多个浏览器同时看共用同一条管线（编码只做一次）。H.264 是流式编码，
    后来接入的客户端拿不到已经过去的帧，必须从下一个关键帧（keyframe-period=60，
    即 1s 一个 IDR）开始才解得动，所以每个新客户端先丢掉流里的残余字节、
    从最近的 MP4 分片边界开始供流。
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_frame = 0.0
        self._error = ""
        self._encoder = ""        # "vaapi" / "x264" / ""（没探测到）
        self._header = b""        # mp4mux 写在最前面的 ftyp+moov，每个客户端都要先收到
        self._clients: set[asyncio.Queue] = set()
        # 让遮罩在推流期间一直藏着：遮罩是独立窗口，桌面模式下会混进画面
        self._lift: asyncio.Task | None = None

    # ---- 状态 ----

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict:
        w, h = _RENDER.size or (0, 0)
        return {
            "available": bool(shutil.which("gst-launch-1.0")),
            "running": self.running,
            "clients": len(self._clients),
            "fps": DEFAULT_FPS,
            "encoder": self._encoder,
            "bitrate_kbps": VAAPI_KBPS,
            "width": w,
            "height": h,
            "last_frame": self._last_frame,
            "error": self._error,
        }

    def set_size(self, width: int, height: int) -> None:
        """前端播放器解出实际分辨率后回报，角标据此显示"""
        if width > 0 and height > 0:
            _RENDER.size = (width, height)

    # ---- 起停管线 ----

    def _cmd(self, node: str, fps: int, quality: int, enc: str) -> list[str]:
        """拼 gst 管线：H.264 硬编/软编 → MP4 分片 → stdout

        为什么不是 MJPEG：MJPEG 每帧都是一张独立 JPEG，60fps 全图编码要吃掉半个核。
        H.264 只编帧间差异，vaapi 走 AMD VCN 固定单元，CPU 几乎不参与，
        代价是浏览器端必须用 <video> 播（<img> 只认 multipart 的图片流）。
        """
        head = [
            "gst-launch-1.0", "-q",
            "pipewiresrc", f"path={node}",
            "!", "videoconvert", "!",
            # 帧率上限：掌机上控制发热，静止画面也不白烧编码器
            "videorate", "!",
            f"video/x-raw,framerate={fps}/1", "!",
        ]
        if enc == "vaapi":
            # vaapih264enc 的 bitrate 单位是 kbps，quality-level 1 最快（延迟最低）
            enc_part = [
                "vaapipostproc", "!",
                "video/x-raw,format=NV12", "!",
                "vaapih264enc", "rate-control=cbr",
                f"bitrate={VAAPI_KBPS}", "keyframe-period=60",
                "quality-level=1", "!",
            ]
        else:
            enc_part = [
                "x264enc", "tune=zerolatency", "speed-preset=ultrafast",
                f"bitrate={quality * 20}", "key-int-max=60", "!",
            ]
        tail = [
            # 关键的一步：用 fragmented MP4 而不是裸 H.264。
            # 裸流没有容器，<video> 拿不到时长/moov，浏览器会当损坏文件直接不播；
            # fragmented 模式下 moov 在最前面、每帧都是可解析的 moof+mdat，
            # 边下边播正是 MSE / 原生 video 支持的方式。
            "h264parse", "config-interval=-1", "!",
            "mp4mux", "fragment-duration=100", "streamable=true", "!",
            "fdsink", "sync=false",
        ]
        return head + enc_part + tail

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            self._error = ""
            if not shutil.which("gst-launch-1.0"):
                self._error = "缺少 gst-launch-1.0（gstreamer1.0-tools 未安装）"
                raise RuntimeError(self._error)
            node = await gamescope_pw_node()
            if not node:
                self._error = "没有找到 gamescope 的画面节点（仅游戏模式可用）"
                raise RuntimeError(self._error)
            sess = _session_user()
            if not sess:
                self._error = "找不到登录的图形会话（/run/user 下没有可用用户）"
                raise RuntimeError(self._error)
            uid, user = sess
            env = dict(os.environ)
            # 必须指向 gamescope 所属会话，root 时默认是 /run/user/0，连不上
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
            # root 跑时降权：PipeWire 的 socket 与 /dev/dri 权限都按用户走
            wrap = ["runuser", "-u", user, "--"] if os.getuid() == 0 and uid != 0 else []

            # 按「最终运行身份」实测一遍编码器，别在 root 视角下误判
            enc = await asyncio.to_thread(_probe_encoder, env, wrap)
            if not enc:
                self._error = ("没有可用的 H.264 编码器（vaapi 与 x264enc 都起不来，"
                               "需要 gstreamer1.0-vaapi 或 gstreamer1.0-plugins-ugly）")
                raise RuntimeError(self._error)
            self._encoder = enc
            self._header = b""
            cmd = self._cmd(node, DEFAULT_FPS, DEFAULT_QUALITY, enc)
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *wrap, *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                )
            except Exception as exc:
                self._error = f"推流管线启动失败：{exc}"
                raise RuntimeError(self._error)
            self._reader = asyncio.create_task(self._pump())
            self._lift = asyncio.create_task(self._keep_lifted())
            try:
                await self._wait_first_frame()
            except Exception:
                await self.stop()
                raise

    async def _wait_first_frame(self) -> None:
        deadline = time.time() + _FIRST_FRAME_TIMEOUT
        while time.time() < deadline:
            if self._last_frame:
                return
            if not self.running:
                break
            await asyncio.sleep(0.05)
        if not self._last_frame:
            self._error = self._error or "推流管线起来了，但等不到第一个分片"
            raise RuntimeError(self._error)

    async def stop(self) -> None:
        async with self._lock:
            for task in (self._reader, self._lift):
                if task is not None:
                    task.cancel()
            self._reader = self._lift = None
            proc, self._proc = self._proc, None
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            self._last_frame = 0.0
            # 客户端队列留着，前端重连后还能收到新帧

    # ---- 切分 MP4 ----

    async def _pump(self) -> None:
        """把 gst stdout 的字节流按 MP4 box 切开，逐个 box 转发

        mp4mux 的输出是标准 ISO BMFF：`[size:4][type:4][payload]` 依次排列，
        ftyp/moov 只在开头出现一次，之后每个 fragment（duration 100ms）
        都是一个 moof+mdat。按 box 整块转发，客户端任意时刻接入都能从
        moof 边界开始解析，不会被撕成半个 box。
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buf = b""
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 8:
                    size = int.from_bytes(buf[:4], "big")
                    if size < 8:
                        # size==1 是 64 位长度（type 后 8 字节），mp4mux 不用；
                        # 出现 0 或非法值说明流错位了，丢掉重来
                        if size == 1 and len(buf) >= 16:
                            size = int.from_bytes(buf[8:16], "big")
                        else:
                            buf = b""
                            break
                    if len(buf) < size:
                        break
                    box = buf[:size]
                    buf = buf[size:]
                    self._feed(box)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._error = f"读取推流失败：{exc}"
        # 管线自己退了（游戏退出 / gst 报错）：清掉状态，前端据此提示重连
        if self._proc is proc:
            self._error = self._error or "推流管线已退出"
            self._proc = None

    def _feed(self, box: bytes) -> None:
        kind = box[4:8]
        if kind in (b"ftyp", b"moov"):
            # 头部（含 SPS/PPS）要缓存下来：后接入的客户端必须先从它开始
            self._header += box
            return
        self._last_frame = time.time()
        for q in list(self._clients):
            try:
                q.put_nowait(box)
            except asyncio.QueueFull:
                # 慢客户端：丢掉它最旧的一块，不要让它拖住其他人
                try:
                    q.get_nowait()
                    q.put_nowait(box)
                except Exception:
                    pass

    # ---- 客户端订阅 ----

    def subscribe(self, maxsize: int = 3) -> asyncio.Queue:
        # 留 3 块：一块约 100ms。队列太深会让延迟越积越大，太浅则抖一下
        # 就丢 fragment，丢掉的那几百毫秒画面会卡住等下一个关键帧
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def mp4(self):
        """产出 fragmented MP4 字节流；最后一个客户端断开后自动停管线"""
        await self.start()
        q = self.subscribe()
        try:
            # 先发缓存的 ftyp+moov：<video> 必须先拿到 moov 里的 SPS/PPS 才能解码
            if self._header:
                yield self._header
            while True:
                try:
                    box = await asyncio.wait_for(q.get(), timeout=_STALL_SECONDS)
                except asyncio.TimeoutError:
                    if not self.running:
                        return
                    # 管线还在但很久没数据：不主动断，继续等（游戏切场景时有长间隔）
                    continue
                yield box
        finally:
            self.unsubscribe(q)
            if not self._clients:
                await self.stop()

    # ---- 遮罩 ----

    async def _keep_lifted(self) -> None:
        """推流期间持续把遮罩抬高（藏着）。

        遮罩只在「会动屏幕」的工具执行时出现，而那种时刻恰恰是最需要看画面的；
        它一显示就会盖在游戏画面上（桌面模式还会混进推流），所以这里每 0.5s
        重申一次 lift，工具收尾时把 lift 复位也追不上这个节奏。
        """
        import tools

        try:
            while True:
                if tools.ON_OVERLAY_LIFT:
                    try:
                        await tools.ON_OVERLAY_LIFT(True)
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        finally:
            if tools.ON_OVERLAY_LIFT:
                try:
                    await tools.ON_OVERLAY_LIFT(False)
                except Exception:
                    pass


STREAM = Streamer()
