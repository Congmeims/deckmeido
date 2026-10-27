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
import hashlib
import inspect
import itertools
import hmac
import json
import os
import re
import secrets
import shutil
import time
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

# 面板底部显示版本号：优先用后端 config 里报的版本（和网页设置页一致），
# 后端连不上时退回插件自己的 package.json
_PLUGIN_VERSION = "0.1.0"
try:
    _pkg = json.loads((Path(__file__).resolve().parent / "package.json").read_text(encoding="utf-8"))
    _PLUGIN_VERSION = str(_pkg.get("version") or _PLUGIN_VERSION)
except Exception:
    pass


def _load_server_cfg() -> dict:
    """读后端的 [server] 段，拿 port 和 token"""
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("server") or {}
    except Exception:
        return {}


# ---------- 本机鉴权 ----------
#
# 插件后端以 root 加载，get_status / screenshot / gamepad_press 这些方法一旦
# 被别的东西调起来，等于把「root 写 /dev/uinput」「读游戏画面」直接送出去。
# 这里加一道令牌门：插件的调用方必须先在 QAM 里解锁，之后每次调用带上会话票据。
#
# 令牌默认 meido（和后端 config.example.toml 的默认值一致，用户只需记一个）。
# 票据是解锁时随机生成的，存在插件数据目录里，Decky 重启后不用重新输入。

DEFAULT_PLUGIN_TOKEN = "meido"

# 暴力猜令牌的限制：连续错这么多次就冷却
_MAX_FAILS = 5
_FAIL_COOLDOWN = 30.0
_fails: list[float] = []

_SESSION_PATH: Path | None = None
_SESSION_TICKET = ""
_TICKET_TTL = 12 * 3600.0
_ticket_at = 0.0


def _session_path() -> Path:
    """票据落盘位置：插件数据目录（decky.DECKY_PLUGIN_SETTINGS_DIR）"""
    global _SESSION_PATH
    if _SESSION_PATH is not None:
        return _SESSION_PATH
    cand = ""
    try:
        cand = str(getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", "") or "")
    except Exception:
        cand = ""
    if not cand:
        # 老版本 Decky 没有这个属性，退到用户目录下的隐藏目录
        cand = str(Path.home() / ".deckmeido")
    p = Path(cand)
    try:
        p.mkdir(parents=True, exist_ok=True)
        # 票据只有属主能读：root 跑的时候别让 deck 读到
        os.chmod(p, 0o700)
    except Exception:
        pass
    _SESSION_PATH = p / "qam_session.json"
    return _SESSION_PATH


def _load_ticket() -> str:
    """读上次解锁留下的票据（没有或过期就当没解锁）"""
    global _SESSION_TICKET, _ticket_at
    if _SESSION_TICKET:
        return _SESSION_TICKET
    try:
        data = json.loads(_session_path().read_text(encoding="utf-8"))
        ticket = str(data.get("ticket") or "")
        at = float(data.get("at") or 0)
    except Exception:
        return ""
    if not ticket or time.time() - at > _TICKET_TTL:
        return ""
    _SESSION_TICKET, _ticket_at = ticket, at
    return ticket


def _save_ticket(ticket: str):
    global _SESSION_TICKET, _ticket_at
    _SESSION_TICKET = ticket
    _ticket_at = time.time()
    path = _session_path()
    try:
        path.write_text(
            json.dumps({"ticket": ticket, "at": _ticket_at}), encoding="utf-8"
        )
        os.chmod(path, 0o600)
    except Exception as exc:
        decky.logger.warning(f"票据落盘失败（本次仍可用）：{exc}")


def _clear_ticket():
    global _SESSION_TICKET, _ticket_at
    _SESSION_TICKET, _ticket_at = "", 0.0
    try:
        _session_path().unlink(missing_ok=True)
    except Exception:
        pass


def _token() -> str:
    """插件自己的令牌：环境变量 > 插件配置 > 默认 meido。

    环境变量 DECKMEIDO_QAM_TOKEN 优先，方便排查时临时换一个，不用改盘上的配置；
    插件配置指 <插件设置目录>/plugin.toml 里的 [plugin] qam_token。
    """
    env = os.environ.get("DECKMEIDO_QAM_TOKEN", "").strip().lower()
    if env:
        return env
    settings = str(getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", "") or "")
    if settings:
        try:
            cfg = tomllib.loads((Path(settings) / "plugin.toml").read_text(encoding="utf-8"))
            val = str((cfg.get("plugin") or {}).get("qam_token") or "").strip().lower()
            if val:
                return val
        except Exception:
            pass
    return DEFAULT_PLUGIN_TOKEN


def _same(a: str, b: str) -> bool:
    """定长比较，避免按字符猜令牌"""
    return hmac.compare_digest(
        hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest()
    )


def _unlocked() -> bool:
    """当前是否有有效的解锁票据"""
    if not _load_ticket():
        return False
    if time.time() - _ticket_at > _TICKET_TTL:
        _clear_ticket()
        return False
    return True


def _require(ticket: str, method: str):
    """每个受保护方法进来先过这道门，没过就抛，前端会收到错误字符串"""
    if not _unlocked():
        raise PermissionError(f"{method}: 未解锁，先在 QAM 面板输入访问令牌")
    if not _same(str(ticket or ""), _SESSION_TICKET):
        raise PermissionError(f"{method}: 票据不对，请重新解锁")


def _too_many_fails() -> int:
    """返回还要等几秒才能重试；0 表示可以试"""
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < _FAIL_COOLDOWN]
    if len(_fails) < _MAX_FAILS:
        return 0
    return int(_FAIL_COOLDOWN - (now - _fails[0])) + 1


# ---------- 当前在玩的游戏 ----------
#
# 查攻略时「哪个游戏」比画面本身还关键：认错了游戏，搜回来的攻略全废。
# 所以不靠模型猜，按可靠度从高到低自己找：
# 1. 运行中进程的 SteamAppId 环境变量 —— Steam 启动游戏时会注入，最准
# 2. 按 appid 去 Steam 自己的配置里取名字 —— Steam 主页（按 Steam 键弹出的
#    界面）左上角显示的就是这个字段，所以名字必然和菜单上一致：
#    Steam 游戏在 localconfig.vdf 的 apps.<appid>.name（用户在库里改的备注也是它），
#    非 Steam 游戏在 shortcuts.vdf 的 AppName
# 3. 非 Steam 游戏的 SteamAppId 是 0，用 compatdata 目录名当编号再查（见下）
# 4. 都拿不到就交给模型看画面
#
# 没走「读菜单上的字」那条路：菜单是 gamescope 合成层，抓帧要把 QAM 收起来
# 而且得等动画，拿到一屏图像还得做 OCR 才有一行文本；直接读配置文件
# 是同一份数据源，更快也更准。


def _read_vdf_apps(path: Path) -> dict[str, str]:
    """从 localconfig.vdf 里抽出 apps { appid: name }

    VDF 是嵌套的花括号结构，app 段里还夹着 cloud 之类的小块，
    所以不能按「遇到第一个 } 就算完」来切，得做括号配对。
    """
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return {}
    apps: dict[str, str] = {}

    head = re.search(r'"apps"\s*\{', text)
    if not head:
        return apps
    depth, i = 1, head.end()
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    seg = text[head.end():i - 1]

    for km in re.finditer(r'"(\d{2,10})"\s*\{', seg):
        j, d = km.end(), 1
        while j < len(seg) and d:
            if seg[j] == "{":
                d += 1
            elif seg[j] == "}":
                d -= 1
            j += 1
        nm = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', seg[km.end():j - 1])
        if nm:
            apps[km.group(1)] = nm.group(1)
    return apps


# 二进制 VDF（shortcuts.vdf）的类型字节
_BVDict_NEST, _BVDict_STR, _BVDict_INT, _BVDict_END = 0x00, 0x01, 0x02, 0x08


def _read_shortcuts(path: Path) -> dict[str, str]:
    """从 shortcuts.vdf（二进制）里抽出「添加的非 Steam 游戏」appid → AppName

    这个文件和 localconfig.vdf 不是一回事：它是二进制 VDF，
    类型字节 + 以 \\0 结尾的键名，值再按类型读。结构详见
    Valve 开发者 Wiki 的 Steam Library Shortcuts 条目：
    root「shortcuts」→ 若干「0/1/2…」→ 每项里有 appid(int32) 和 AppName(string)。

    库里改过的名字就是这里的 AppName，Steam 界面显示的就是它。
    注意改过名之后 appid 不会重算（还是当初 crc32(原名+exe) 的值），
    所以只能按 appid 精确匹配查 AppName，反推是行不通的。
    """
    try:
        data = path.read_bytes()
    except Exception:
        return {}
    out: dict[str, str] = {}

    def cstr(pos: int) -> tuple[str, int]:
        end = data.index(b"\0", pos)
        return data[pos:end].decode("utf-8", "replace"), end + 1

    def skip_dict(pos: int) -> int:
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == _BVDict_END:
                return pos
            key, pos = cstr(pos)
            if t == _BVDict_NEST:
                pos = skip_dict(pos)
            elif t == _BVDict_STR:
                _, pos = cstr(pos)
            elif t == _BVDict_INT:
                pos += 4
            else:
                return pos
        return pos

    try:
        pos = 0
        if data[pos] != _BVDict_NEST:
            return {}
        pos += 1
        root, pos = cstr(pos)
        if root != "shortcuts":
            return {}
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == _BVDict_END:
                break
            _, pos = cstr(pos)  # "0"/"1"/…
            appid, name = "", ""
            while pos < len(data):
                t2 = data[pos]
                pos += 1
                if t2 == _BVDict_END:
                    break
                k, pos = cstr(pos)
                if t2 == _BVDict_NEST:
                    pos = skip_dict(pos)
                elif t2 == _BVDict_STR:
                    v, pos = cstr(pos)
                    if k == "AppName":
                        name = v
                elif t2 == _BVDict_INT:
                    raw = data[pos:pos + 4]
                    pos += 4
                    if k == "appid" and len(raw) == 4:
                        # 有符号存的，Steam 界面按无符号十位显示
                        appid = str(int.from_bytes(raw, "little", signed=False))
                else:
                    break
            if appid and name:
                out[appid] = name
    except (ValueError, IndexError):
        # 文件被 Steam 写了一半或格式不认识：能捞多少算多少，别炸
        pass
    return out


def _steam_homes(home: Path) -> list[Path]:
    """Steam 安装目录的候选位置

    Deck 上 ~/.steam/steam 是个软链，正常都在，但装到别处（或改了用户目录）时
    会落在 XDG 的默认位置，所以多列几处。
    """
    return [
        home / ".steam/steam",
        home / ".local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]


def _all_userdata() -> list[Path]:
    """所有 Steam 账号的 userdata 目录

    Steam 主页左上角那个游戏名，读的就是这套数据：Steam 游戏走
    localconfig.vdf 的 apps.<appid>.name，非 Steam 游戏走 shortcuts.vdf 的
    AppName。所以直接把这两处拼起来，等于拿到和菜单上一样的名字，
    不用去截菜单画面或读屏幕文字。
    """
    home = INSTALL_DIR.parent
    out: list[Path] = []
    for steam in _steam_homes(home):
        ud = steam / "userdata"
        if ud.is_dir():
            out.extend(p for p in ud.iterdir() if p.is_dir())
    # install.sh 给 deckmeido 建了桌面快捷方式，说明这台机器的 XDG 用户目录
    # 被指到别处了（比如 Documents/deckmeido），Steam 也可能跟着挪了窝
    try:
        docs = (home / ".config/user-dirs.dirs").read_text(errors="ignore")
        m = re.search(r'XDG_DOCUMENTS_DIR="([^"]+)"', docs)
        if m:
            nav = Path(m.group(1).replace("$HOME", str(home))) / "steam/nav"
            out.extend(p for p in nav.iterdir() if p.is_dir())
    except Exception:
        pass
    return out


def _app_names() -> dict[str, str]:
    """汇总所有 Steam 账号的 appid → 名字（含非 Steam 游戏的备注名）"""
    out: dict[str, str] = {}
    for ud in _all_userdata():
        cfg = ud / "config" / "localconfig.vdf"
        for k, v in _read_vdf_apps(cfg).items():
            out.setdefault(k, v)
        # 非 Steam 游戏只存在 shortcuts.vdf 里，localconfig 里没有它们
        for k, v in _read_shortcuts(ud / "config" / "shortcuts.vdf").items():
            out.setdefault(k, v)
    return out


def _compat_app_id(compat_path: Path) -> list[str]:
    """由 compatdata/<编号> 目录推出可能的 appid 写法

    非 Steam 游戏没有商店 appid，Steam 拿 shortcuts.vdf 里那个 int32 当它的编号，
    界面按无符号显示成十位数字（如 2914928358），compatdata 目录名也是它。
    但目录名到底写成有符号还是无符号，不同 Steam 版本还没统一，
    所以两种都返回，交给 _app_names() 去命中。
    """
    name = compat_path.name.strip()
    if not name.lstrip("-").isdigit():
        return []
    n = int(name)
    out = [name]
    if n < 0:  # 写成了负数（-1380038938），换回无符号十位再试一次
        alt = n & 0xFFFFFFFF
        if str(alt) not in out:
            out.append(str(alt))
    return out


def _running_app_ids() -> list[str]:
    """扫 /proc 找 SteamAppId，排除 Steam 自己和 gamescope

    只读 environ，不读 cmdline：命令行里常带 store 页面之类的 appid，会把结果带偏。
    非 Steam 游戏的 SteamAppId 是 0，只能靠 STEAM_COMPAT_DATA_PATH 的目录名认。
    """
    ids: list[str] = []
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return ids
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except Exception:
            continue
        compat = ""
        for kv in env:
            if kv.startswith(b"SteamAppId="):
                val = kv.split(b"=", 1)[1].decode(errors="ignore").strip()
                if val and val != "0":
                    ids.append(val)
            elif kv.startswith(b"STEAM_COMPAT_DATA_PATH="):
                compat = kv.split(b"=", 1)[1].decode(errors="ignore").strip()
        if compat:
            # 变量指向 .../compatdata/<编号>，有时后面还跟着 /pfx
            ids.extend(_compat_app_id(Path(compat.rstrip("/").removesuffix("/pfx"))))
    return ids


def _current_game() -> dict:
    """返回 {appid, name}，找不到就 name 为空"""
    ids = _running_app_ids()
    if not ids:
        return {"appid": "", "name": ""}
    names = _app_names()
    # 同一个游戏会有好几个子进程，环境变量一致；取第一个能查到名字的。
    # 非 Steam 游戏（库里那个十位编号）走的就是这条路：compatdata 目录名
    # 命中 shortcuts.vdf 的 appid，拿到的是用户在库里改过的 AppName。
    for appid in ids:
        if names.get(appid):
            return {"appid": appid, "name": names[appid]}
    return {"appid": ids[0], "name": ""}


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
_ABS_X, _ABS_Y, _ABS_Z, _ABS_RX, _ABS_RY, _ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
_BTN_SOUTH, _BTN_EAST, _BTN_NORTH, _BTN_WEST = 0x130, 0x131, 0x133, 0x134
_BTN_TL, _BTN_TR = 0x136, 0x137
_BTN_THUMBL, _BTN_THUMBR = 0x13D, 0x13E
# Series X|S 的分享键。内核里没有专用 BTN_*，xpad 驱动把它报成 KEY_RECORD
# （HID consumer 0xB2），所以这里也按键盘键注册
_KEY_RECORD = 0x0A7

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
    # BTN_MODE：Xbox 手柄的 Guide 键
    "guide": 0x13C,
    # 摇杆按下（L3 / R3）
    "l3": _BTN_THUMBL,
    "r3": _BTN_THUMBR,
    # 分享键：Xbox Series X|S 才有，部分驱动/游戏不认
    "share": _KEY_RECORD,
}

# 扳机名字 → 轴。按手柄惯例走 0..32767
_GAMEPAD_TRIGGERS = {"lt": _ABS_Z, "rt": _ABS_RZ}


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
                # 右摇杆：不少游戏用右摇杆转视角，没有它只能动左摇杆
                (_ABS_RX, _STICK_MIN, _STICK_MAX),
                (_ABS_RY, _STICK_MIN, _STICK_MAX),
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

    async def stick(self, x: float, y: float, side: str = "left") -> dict:
        """摇杆，两个参数都是 -1..1（1 是右/下）。side 取 left / right"""
        axes = (_ABS_RX, _ABS_RY) if str(side).lower() == "right" else (_ABS_X, _ABS_Y)
        try:
            vx = int(max(-1.0, min(1.0, float(x))) * _STICK_MAX)
            vy = int(max(-1.0, min(1.0, float(y))) * _STICK_MAX)
        except (TypeError, ValueError):
            return {"ok": False, "error": "摇杆值要是 -1..1 的数字"}
        async with self._lock:
            try:
                if self._fd is None:
                    self._create()
                self._emit(_EV_ABS, axes[0], vx)
                self._emit(_EV_ABS, axes[1], vy)
                self._syn()
            except FileNotFoundError:
                return {"ok": False, "error": "没有 /dev/uinput：uinput 内核模块未加载"}
            except PermissionError:
                return {"ok": False, "error": "打不开 /dev/uinput：插件需要以 root 加载"}
            except Exception as exc:
                return {"ok": False, "error": f"摇杆注入失败：{exc}"}
            self._touch_idle()
        return {"ok": True, "x": vx, "y": vy}

    async def trigger(self, which: str, value: float = 1.0) -> dict:
        """扳机力度：value 是 0..1（0 松开、1 到底）。松开请显式传 0"""
        name = str(which or "").strip().lower()
        code = _GAMEPAD_TRIGGERS.get(name)
        if code is None:
            return {"ok": False, "error": f"不认识的扳机：{which}（可用：lt、rt）"}
        try:
            level = int(max(0.0, min(1.0, float(value))) * _TRIG_MAX)
        except (TypeError, ValueError):
            return {"ok": False, "error": "扳机力度要是 0..1 的数字"}
        async with self._lock:
            try:
                if self._fd is None:
                    self._create()
                self._emit(_EV_ABS, code, level)
                self._syn()
            except FileNotFoundError:
                return {"ok": False, "error": "没有 /dev/uinput：uinput 内核模块未加载"}
            except PermissionError:
                return {"ok": False, "error": "打不开 /dev/uinput：插件需要以 root 加载"}
            except Exception as exc:
                return {"ok": False, "error": f"扳机注入失败：{exc}"}
            self._touch_idle()
        return {"ok": True, "trigger": name, "value": level}

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

    def __init__(self, url: str, user_agent: str = ""):
        self.url = url
        self.user_agent = user_agent
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
            + (f"User-Agent: {self.user_agent}\r\n" if self.user_agent else "")
        ) + "\r\n"
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
    """一条连到后端的常驻 ws。

    只用来「发」：每一轮带 turn_id 发出去，订阅由 _TailWS 单独负责。
    """

    def __init__(self, url: str):
        self.url = url
        self._impl = None
        self._send_lock = asyncio.Lock()
        # 本端这一轮的 sid：history 回来时填上，之后靠它认出哪些 δ 是自己的
        self.sid = ""

    async def open(self):
        # 后端用 UA 判断「这条连接是网页还是手机」，插件不声明就会被当成一个
        # 额外的网页端，界面上白多出一栏来源。标成 QAM 才认得出是谁
        hdr = {"User-Agent": "DeckMeido-QAM/1.0"}
        if _ws_connect is not None:
            self._impl = await _ws_connect(
                self.url, max_size=32 * 1024 * 1024, open_timeout=10,
                additional_headers=hdr,
            )
        else:
            self._impl = await _RawWSClient(self.url, user_agent=hdr["User-Agent"]).__aenter__()

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
# 面板展示的对话：所有端的提问和回复，{role, text, client, ts}
_THREAD: list[dict] = []
_THREAD_MAX = 200     # 只留最近这么多条，再往前翻也用不上
# 最近一次推给面板的 thread 快照，等界面 ready 时补发（见 _emit）
_LAST_THREAD: dict | None = None
# 面板展示的来源标记，和后端、网页端的叫法保持一致
_CLIENT_LABEL = {"web": "网页", "mobile": "手机", "client": "客户端", "qam": "插件"}


def _push_thread(item: dict):
    """追加一条并推给面板。

    user_message 的 ts 在历史里唯一，重复推送（重连后补发）按 ts 去掉。
    """
    ts = float(item.get("ts") or 0)
    for m in (_THREAD[-8:] if ts else []):
        if m.get("role") == item["role"] and abs(float(m.get("ts") or 0) - ts) < 1e-6:
            return
    # ts 为 0 的是「本地先落、还没拿到后端时间戳」的那条（见 start_turn）：
    # 后端回声在 ts 上对不上，按角色+正文把本地占位换掉
    if ts:
        for m in reversed(_THREAD[-8:]):
            if (m.get("role") == item["role"] and not float(m.get("ts") or 0)
                    and str(m.get("text") or "") == str(item.get("text") or "")):
                m.update(item)
                _emit({"kind": "thread", "items": _THREAD})
                return
    _THREAD.append(item)
    if len(_THREAD) > _THREAD_MAX:
        del _THREAD[: len(_THREAD) - _THREAD_MAX]
    _emit({"kind": "thread", "items": _THREAD})


def _sync_thread(hist: list):
    """用后端给的完整历史对齐本地列表（连上时、别的端改会话时）

    只看文本消息：随图那条多模态消息（content 是数组）不进面板。
    """
    out = []
    for m in hist:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            continue
        out.append({
            "role": m["role"],
            "text": str(content or ""),
            "client": str(m.get("client") or ""),
            "ts": float(m.get("ts") or 0),
        })
    _THREAD[:] = out[-_THREAD_MAX:]
    _emit({"kind": "thread", "items": _THREAD})


def _backend_url(port: int, token: str) -> str:
    return f"ws://127.0.0.1:{port}/ws?token={token}"


def _emit(msg: dict):
    """把一条消息推给 QAM（前端 addEventListener("deckmeido_delta")）

    界面可能比面板开关得晚（Decky 在面板首帧之后才挂监听），后端连得早时
    history 那一条 thread 会落空，所以这里顺手留一份最新快照，等面板调
    ready() 再补发。

    新版 Decky 的 decky.emit 是协程，直接调用什么都不会发出去（只在日志里
    留一条 RuntimeWarning），连当前事件循环也要忙到下一次 await 才第一次真正
    执行 —— 所以这里必须挂到循环上跑，不能当同步函数使。
    """
    global _LAST_THREAD
    if msg.get("kind") == "thread":
        _LAST_THREAD = msg
    try:
        res = decky.emit("deckmeido_delta", msg)
    except Exception as exc:
        decky.logger.warning(f"推送前端失败：{exc}")
        return
    if inspect.isawaitable(res):
        try:
            asyncio.get_running_loop().create_task(res)
        except RuntimeError:
            # 没有运行中的循环（不该发生），至少别把结果丢掉
            decky.logger.warning("推送前端失败：没有事件循环")


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
    """常驻读：history 对齐列表，之后所有端（客户端/网页/手机/插件）的消息都进面板

    后端把用户提问和助手回复广播给每一条 ws 连接，所以这里能拿到别的端的对话。
    本端发出去的那一轮还要额外走 delta / final，把那几十秒的流式过程显示出来。
    """
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
        sid = str(msg.get("sid") or "")
        mine = bool(sid) and sid == bk.sid
        if typ == "history":
            if not bk.sid:
                bk.sid = sid
            _sync_thread(msg.get("messages") or [])
            continue
        if typ == "status":
            # 状态广播不带 sid（它在改配置时也会发），所以放在 sid 过滤之前
            _emit({"kind": "status", "running": bool((msg.get("data") or {}).get("running"))})
            continue
        if typ == "user_message":
            _push_thread({
                "role": "user",
                "text": str(msg.get("text") or ""),
                "client": str(msg.get("client") or ""),
                "ts": float(msg.get("ts") or 0),
            })
            continue
        if typ == "assistant_message":
            # 助手消息现在也带来源（后端按同一轮的提问打标）；老数据没有这个字段，
            # 退回「最近一条 user 的 client」来归户
            _push_thread({
                "role": "assistant",
                "text": str(msg.get("text") or ""),
                "client": str(msg.get("client") or _nearby_client()),
                "ts": float(msg.get("ts") or 0),
            })
            if not mine:
                continue
            if msg.get("cancelled"):
                # 用户点了终止：已生成的部分比「已终止当前操作」有用，保留它
                _ACC = _ACC or str(msg.get("text") or "已终止当前操作")
                _finalize(cancelled=True)
            else:
                # 唯一带完整正文的收尾消息
                _ACC = str(msg.get("text") or _ACC)
                _finalize()
            continue
        if not mine:
            # 别的会话的流式过程与本端这一轮无关，正文已由 assistant_message 收下
            continue
        if typ == "delta":
            text = str(msg.get("text") or "")
            if text:
                _ACC += text
                _emit({"kind": "delta", "text": text})
            if msg.get("final"):
                # 正文已完整，但先不冻结：后面还有一条 assistant_message 要覆盖它
                _ACC = str(msg.get("reply") or _ACC)
        elif typ == "tool_activity":
            # 「正在读文件：…」这类过程提示，只给界面看，不进正文
            _emit({"kind": "tool", "text": str(msg.get("text") or "")})


def _nearby_client() -> str:
    """找最近一条 user 消息的来源，当作这轮回复的归属"""
    for m in reversed(_THREAD):
        if m.get("role") == "user":
            return str(m.get("client") or "")
    return ""


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
    # 等 history：拿到 sid 才能认出哪条 δ 是本端这一轮的
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


def _lan_ip() -> str:
    """本机在局域网里的地址，给别的端填后端地址用

    UDP connect 不发包，只是让内核挑一条默认出口并告诉我们源地址，
    比解析 ip addr 稳（没有默认路由时退回 127.0.0.1）。
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


async def _core_http(port: int, token: str, method: str, path: str,
                     timeout: float = 5.0) -> dict:
    """向后端的本机接口发一个请求，返回 {"ok": bool, "data": {...} | "error": str}

    不走 ws：这类请求都只要一个结论，HTTP 一步拿到，也顺带验证了 token 是
    后端认的（ws 握手失败时前端只会看到连不上）。
    """
    if not token:
        return {"ok": False, "error": "配置里没有 token"}
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except Exception as exc:
        return {"ok": False, "error": f"连不上 :{port}（{exc}）"}
    try:
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Token: {token}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    head, _, body = raw.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        return {"ok": False, "error": head.split(b"\r\n", 1)[0].decode("utf-8", "replace")[:60]}
    try:
        return {"ok": True, "data": json.loads(body)}
    except Exception:
        return {"ok": False, "error": "返回内容不是 JSON"}


async def _core_status(port: int, token: str) -> dict:
    """问一次后端自己的 /api/status，确认女仆核心活着（在不在、什么模式）"""
    return await _core_http(port, token, "GET", "/api/status")


async def _core_prefs(port: int, token: str) -> dict:
    """读后端的偏好项（语音播报、版本号），面板设置区要显示/切换这些"""
    r = await _core_http(port, token, "GET", "/api/config")
    if not r["ok"]:
        return {"ok": False, "error": r.get("error", ""), "tts": False, "version": ""}
    d = r.get("data") or {}
    return {"ok": True, "tts": bool(d.get("tts_enable")), "version": str(d.get("version") or "")}


class Plugin:
    async def get_status(self) -> dict:
        """QAM 打开时先问一次：后端连得上吗、截图能不能用

        这个方法故意不要求解锁：前端要先知道「解锁了没」才能决定显示
        输入框还是正常面板，而且它只报能力，不含任何画面或对话内容。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        token = str(cfg.get("token", "") or "").strip()
        node, why = await _gamescope_pw_node()
        core = await _core_status(port, token)
        prefs = await _core_prefs(port, token) if core.get("ok") else {}
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
            # 面板第二行显示成「192.168.x.x:18765」，别的设备照这个地址连
            "ip": _lan_ip(),
            # 女仆核心（后端自身的 /api/status）在不在、跑没跑起来
            "core_ok": bool(core.get("ok")),
            "core_reason": str(core.get("error") or ""),
            "core_mode": str((core.get("data") or {}).get("mode") or ""),
            # 设置区：语音播报开关（由后端 tts.enable 决定）与版本号（底部显示）
            "tts": bool(prefs.get("tts")),
            "version": str(prefs.get("version") or _PLUGIN_VERSION),
            # 虚拟手柄能不能用：设备节点在 + 进程有写权限
            "gamepad": os.access("/dev/uinput", os.W_OK) if os.path.exists("/dev/uinput") else False,
            # 本机鉴权状态：false 时前端只显示解锁框
            "unlocked": _unlocked(),
            # 后端记着的那张票据。前端把它当成「本机已解锁」的凭据接回来：
            # QAM 面板一关一开组件会重挂载、useState 归零，不补回来的话
            # 「截屏询问」先收面板再展开时就成了「明明解锁了却报未解锁」
            "ticket": _SESSION_TICKET if _unlocked() else "",
        }

    async def session_open(self, ticket: str = "") -> dict:
        """面板打开时调一次：连上后端并拉回全部对话（所有端的提问与回复）

        连上后 _read_loop 会先收到 history 并推一条 thread 事件，前端不必等这里的返回。
        """
        _require(ticket, "session_open")
        bk, why = await _ensure_backend()
        if not bk:
            return {"ok": False, "error": why}
        return {"ok": True}

    async def ready(self, ticket: str = "") -> dict:
        """前端挂好监听后调一次：把积攒下来的对话补推过去

        Decky 是先渲染面板、后挂 addEventListener，而 history 那条 thread 可能在
        面板首帧之前就推完了。这里不重新连后端，只把快照再发一遍。
        """
        _require(ticket, "ready")
        if _LAST_THREAD:
            _emit(_LAST_THREAD)
        elif _THREAD:
            _emit({"kind": "thread", "items": _THREAD})
        return {"ok": True}

    async def unlock(self, token: str) -> dict:
        """用访问令牌解锁插件能力，成功后拿到票据，之后的调用都要带上

        连续错 _MAX_FAILS 次会冷却 _FAIL_COOLDOWN 秒：QAM 上的输入框是唯一入口，
        不设限的话可以脚本化穷举 4 位令牌。
        """
        wait = _too_many_fails()
        if wait:
            return {"ok": False, "error": f"错误次数太多，{wait} 秒后再试"}
        if _same(str(token or "").strip().lower(), _token()):
            _fails.clear()
            ticket = secrets.token_urlsafe(24)
            _save_ticket(ticket)
            decky.logger.info("QAM 已解锁")
            return {"ok": True, "ticket": ticket}
        _fails.append(time.time())
        left = max(0, _MAX_FAILS - len(_fails))
        hint = f"，还可以试 {left} 次" if left else f"，冷却 {int(_FAIL_COOLDOWN)} 秒"
        return {"ok": False, "error": f"令牌不对{hint}"}

    async def lock(self, ticket: str = "") -> dict:
        """退出解锁（换人用这台机器时用），票据落盘一并删掉"""
        _require(ticket, "lock")
        _clear_ticket()
        return {"ok": True}

    async def set_tts(self, enabled: bool, ticket: str = "") -> dict:
        """切语音播报：交给后端写入 [tts] enable，热更新、不用重启"""
        _require(ticket, "set_tts")
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        token = str(cfg.get("token", "") or "").strip()
        r = await _core_http(port, token, "POST",
                             f"/api/tts?enabled={'true' if enabled else 'false'}")
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "tts": bool((r.get("data") or {}).get("tts_enable"))}

    async def screenshot(self, ticket: str = "", hide: bool = False) -> dict:
        """抓当前游戏画面

        hide=True 时抓帧前留一段空档，让前端先收起 QAM：gamescope 的 pipewiresrc
        抓的是合成后的画面，QAM 面板本身也在里面，不收起来就只能拍到半屏游戏。
        菜单收起的动画大约 200ms，这里给 450ms 再抓。
        """
        _require(ticket, "screenshot")
        if hide:
            await asyncio.sleep(0.45)
        # 和抓帧一起把当前游戏的 appid/名字带回去：查攻略时要拼进提示词，
        # 单独再开一次接口既慢又容易和画面错位
        game = await asyncio.to_thread(_current_game)
        ok, payload = await grab_screenshot()
        if ok:
            return {"ok": True, "image": payload, "game": game}
        return {"ok": False, "error": payload, "game": game}

    async def gamepad_press(self, key: str, ms: int = 80, ticket: str = "") -> dict:
        """模拟按一下手柄键：a/b/x/y/lb/rb/up/down/left/right/start/select/guide/l3/r3/share"""
        _require(ticket, "gamepad_press")
        return await _GAMEPAD.press(key, ms)

    async def gamepad_stick(self, x: float, y: float, ticket: str = "",
                            side: str = "left") -> dict:
        """摇杆推到 (x, y)，范围 -1..1；推完不会自动回中，
        面板松手时会再调一次 gamepad_stick(0, 0)。side 取 left / right"""
        _require(ticket, "gamepad_stick")
        return await _GAMEPAD.stick(x, y, side)

    async def gamepad_trigger(self, which: str, value: float = 1.0,
                              ticket: str = "") -> dict:
        """扳机力度：which 取 lt / rt，value 是 0..1（0 松开、1 到底）"""
        _require(ticket, "gamepad_trigger")
        return await _GAMEPAD.trigger(which, value)

    async def gamepad_close(self, ticket: str = "") -> dict:
        """收掉虚拟手柄设备（不再需要时才调，避免 Steam 一直以为接了手柄）"""
        _require(ticket, "gamepad_close")
        await _GAMEPAD.close()
        return {"ok": True}

    async def start_turn(
        self, text: str, image: str | None = None, ticket: str = ""
    ) -> dict:
        """把一轮对话发给后端。返回后流式内容通过 deckmeido_delta 推给前端"""
        _require(ticket, "start_turn")
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
        # 提问不等后端回声，先在面板上落一条：后端广播回来时会按 ts 去重，
        # 不会出现两条一模一样的「插件端」
        _push_thread({
            "role": "user",
            "text": text,
            "client": "qam",
            "ts": time.time(),
        })
        return {"ok": True}

    async def wait_final(self, timeout: float = 900, ticket: str = "") -> dict:
        """等本轮收尾，返回完整正文。前端收到 final 事件就不用调这个"""
        _require(ticket, "wait_final")
        try:
            await asyncio.wait_for(_FINAL_EVENT.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"等了 {timeout:.0f}s 还没有答复"}
        return {"ok": True, "text": _LAST_REPLY}

    async def cancel(self, ticket: str = "") -> dict:
        """掐掉这一轮：断开的后端连接会让那边 CancelledError，
        再补一次 /api/cancel，把子进程树一起收掉"""
        _require(ticket, "cancel")
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
        _clear_ticket()
        decky.logger.info("DeckMeido 插件卸载")
