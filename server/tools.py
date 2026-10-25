"""DeckMeido 工具层：供 LLM function calling 调用的本机能力"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import shutil
import signal
import tempfile
from pathlib import Path

import httpx

HTTP = httpx.AsyncClient(timeout=20, follow_redirects=True)

# ---------- 工具中文名（界面提示统一用中文，不暴露内部函数名） ----------

TOOL_CN: dict[str, str] = {
    "get_weather": "查询天气",
    "web_search": "联网搜索",
    "list_installed_games": "查看游戏库",
    "find_game": "搜索商店游戏",
    "check_system_updates": "检查系统更新",
    "install_game": "安装游戏",
    "list_emulator_roms": "扫描模拟器游戏",
    "run_command": "执行命令",
    "desktop": "操作桌面",
    "screenshot": "截取屏幕",
    "list_dir": "查看目录",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "self_update": "更新软件",
}


def cn_name(name: str) -> str:
    """工具中文名，未登记的回退为原名"""
    return TOOL_CN.get(name, name)


# ---------- 工具定义（OpenAI function calling 格式） ----------

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 上海、北京、Tokyo"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网，获取实时信息、新闻、攻略等",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_games",
            "description": "列出本机 Steam 已安装的游戏",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_game",
            "description": "在 Steam 商店搜索游戏，返回名称和 appid（安装游戏前需要先找到 appid）",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "游戏名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_system_updates",
            "description": "检查 SteamOS 系统更新状态",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_game",
            "description": "通过 Steam 客户端下载安装指定 appid 的游戏（会触发真实下载，需用户确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "appid": {"type": "integer", "description": "Steam 游戏 appid"},
                },
                "required": ["appid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_emulator_roms",
            "description": "扫描本机模拟器 ROM 游戏目录（EmuDeck 标准结构 ~/Emulation/roms 等），按机型统计数量。可指定机型过滤，如 switch",
            "parameters": {
                "type": "object",
                "properties": {
                    "system": {"type": "string", "description": "可选，机型目录名，如 switch、ps2、gba"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": f"在设备上执行 bash 命令（通用能力：文件/进程/桌面操作 xdotool/截屏/软件管理等）。环境：SteamOS、桌面模式 X11 DISPLAY=:0、家目录 {Path.home()}。避免交互式参数。启动浏览器/播放器/游戏这类常驻程序直接写命令即可，系统会自动判定已拉起并返回，不用加 nohup 或 &；需要拿到输出的命令才会等待执行完成",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 bash 命令"},
                    "reason": {"type": "string", "description": "一句话说明这条命令要干什么（用于审批展示）"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 300"},
                    "stdin": {"type": "string", "description": "可选，喂给命令标准输入的内容（用于 sudo -S 密码、交互确认 y 等）"},
                },
                "required": ["command", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop",
            "description": ("操作桌面窗口（Wayland 原生程序也能点）。action=focus 切窗口到前台；"
                            "click 在屏幕坐标处左键单击；move 移动光标；type 在当前窗口打字；where 查光标和焦点窗口。"
                            "坐标从 screenshot 画面上读，左上角为 0,0"),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["focus", "click", "move", "type", "where"],
                               "description": "要做的动作"},
                    "target": {"type": "string", "description": "focus 时填窗口标题关键词（如 Kazumi）"},
                    "x": {"type": "integer", "description": "click/move 的屏幕横坐标"},
                    "y": {"type": "integer", "description": "click/move 的屏幕纵坐标"},
                    "text": {"type": "string", "description": "type 时要输入的文本（只支持英文/数字/符号）"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截取当前桌面屏幕画面，直接展示给用户。排查问题、用户要求看屏幕时使用。截图时会自动收起操作遮罩，不需要为了避开遮罩而等待",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容（编程/文件任务先看结构）。默认列出工作区（家目录下的 deckmeido_workspace）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前工作区"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容（带行号，可分段读取大文件）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "offset": {"type": "integer", "description": "起始行号，默认 1"},
                    "limit": {"type": "integer", "description": "读取行数，默认 400"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或整体覆盖写入文件（自动创建父目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确替换文件中的一段文本（修改代码优先用这个，不要整体重写文件）。old 必须在文件里唯一",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old": {"type": "string", "description": "要被替换的原文（需唯一匹配）"},
                    "new": {"type": "string", "description": "替换后的内容"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "self_update",
            "description": ("检查并更新 DeckMeido 自身。action=check 查询有没有新版本；"
                            "action=upgrade 拉取最新代码并重启服务（会短暂断开，需用户确认）"),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["check", "upgrade"],
                               "description": "check 只查版本，upgrade 真的升级"},
                },
                "required": ["action"],
            },
        },
    },
]

# 危险操作：任何权限模式下都需确认
DANGEROUS = {"install_game"}

# 会实际控制设备的工具里，只有真正「动光标 / 敲键盘」的调用才弹操作遮罩。
# 联网搜索、查天气、读写文件、跑普通 shell 命令都只是后台任务，
# 遮罩白挡一层，还会被误当成「正在操作」，所以一律不弹。
#
# focus 刻意不在里面：它只调 kdotool windowactivate/windowraise 激活窗口，
# 光标不动、键盘不敲。窗口一抬起来就盖一层遮罩，用户会以为 agent 在操作设备。
DESKTOP_ACTION_TOOLS = {"click", "move", "type"}

# 真正会注入输入的子命令（xdotool / ydotool / dotool 同名）
_XDOTOOL_INPUT_SUBS = {
    "click", "mousedown", "mouseup",
    "mousemove", "mousemove_relative", "move",
    "type", "key", "keydown", "keyup",
    "drag", "scroll", "wheel", "text",
}
# kdotool 的输入类子命令；window* 系列属于窗口管理，不动光标不敲键盘
_KDOTOOL_INPUT_SUBS = {
    "type", "key", "keydown", "keyup",
    "mousemove", "mousemove_relative", "move",
    "click", "mousedown", "mouseup", "scroll", "wheel", "text",
}
# 会让光标按下去的子命令：这些必须打开遮罩穿透，否则点会落在遮罩上变成「用户终止」
_POINTER_CLICK_SUBS = {"click", "mousedown", "mouseup"}

# 输入注入工具的调用点。只取到本段命令结束（;&| 或换行），
# 避免把后续无关命令里的同名单词也当成子命令
_INPUT_TOOLS_RE = re.compile(
    r"(?:^|[\s|;&(`'\"/])(xdotool|ydotool|dotool|wtype|kdotool)\b([^\n;&|]*)")
# 这些选项后面跟一个值，解析子命令时要连值一起跳掉。
# 少了 --name/--class 这类，「找标题里带 type 的窗口」会被当成要打字
_OPTS_WITH_VALUE = {
    "--delay", "--repeat", "--window", "--terminator", "--args",
    "--name", "--class", "--classname", "--role",
    "--screen", "--desktop", "--limit", "--maxdepth", "--pid", "--windowid",
}

_INPUT_TOOLS = {"xdotool", "ydotool", "dotool", "wtype", "kdotool"}
# 站在命令真正执行位置前面的包装词，剥掉之后才看得到真实程序
_CMD_PREFIXES = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "stdbuf"}
# 按 shell 的分段符切开：只有段首才是「真正被执行的程序」，
# 这样 echo 'xdotool click 1' 只是打印一段字符串，不会被当成点击
_SEGMENT_SPLIT_RE = re.compile(r"(\$\(|\)|;|&&|\|\||\||`|\n)")

# 已知子命令（三个工具取并集，具体哪些算「注入输入」由各工具的集合决定）。
# 只有认得出子命令列表，才能把 `xdotool sleep 1 mousemove 3 3` 这种链式写法里
# 真正的 mousemove 认出来，同时不把 `search --name type` 里的 type 当子命令
_KNOWN_SUBS = {
    "xdotool": {
        "click", "mousedown", "mouseup", "mousemove", "mousemove_relative", "move",
        "type", "key", "keydown", "keyup", "drag", "scroll", "wheel", "text",
        "search", "getactivewindow", "getwindowname", "getwindowgeometry",
        "getwindowpid", "getmouselocation", "getdisplaygeometry", "get_num_desktops",
        "get_desktop", "set_desktop", "set_num_desktops", "windowactivate",
        "windowraise", "windowfocus", "windowmove", "windowsize", "windowkill",
        "windowclose", "windowminimize", "windowunmap", "windowmap", "windowreparent",
        "set_window", "behave", "exec", "sleep", "version", "help",
    },
    "kdotool": {
        "click", "mousedown", "mouseup", "mousemove", "mousemove_relative", "move",
        "type", "key", "keydown", "keyup", "drag", "scroll", "wheel", "text",
        "search", "getactivewindow", "getwindowname", "getmouselocation",
        "windowactivate", "windowraise", "windowclose", "windowminimize",
        "windowmove", "windowresize", "windowkill", "windowlist",
    },
}
_KNOWN_SUBS["ydotool"] = _KNOWN_SUBS["xdotool"] | {"debug", "mousemove_absolute"}
_KNOWN_SUBS["dotool"] = _KNOWN_SUBS["xdotool"] | {"input"}


def _segments(cmd: str) -> list[str]:
    """把命令行按 ; && || | $() 反引号换行切成若干「执行段」"""
    return [s for s in _SEGMENT_SPLIT_RE.split(cmd or "")
            if s and not _SEGMENT_SPLIT_RE.fullmatch(s)]


def _tokens(seg: str) -> list[str]:
    """切词。引号不配对时退回朴素切分，绝不因为解析失败就漏判"""
    try:
        return shlex.split(seg, comments=False, posix=True)
    except Exception:
        return seg.split()


def _strip_prefixes(toks: list[str]) -> list[str]:
    """剥掉 sudo / env / VAR=VALUE 这类前缀，返回从真实程序开始的参数"""
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _CMD_PREFIXES or re.fullmatch(r"[A-Za-z_]\w*=\S*", t) or t.startswith("-"):
            i += 1
            continue
        break
    return toks[i:]


def _parse_subs(tool: str, args: list[str]) -> list[str]:
    """按真实语法顺序解析出一段里出现的全部子命令

    逐个走参数：选项跳过（带值的连值一起跳），认得出的字当子命令，
    认不出的当上一个子命令的参数。这样：
    - `sleep 1 mousemove 3 3`  → ['sleep', 'mousemove']   链式写法不掉
    - `search --name type`     → ['search']               窗口标题不当子命令
    """
    known = _KNOWN_SUBS.get(tool, set())
    subs: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            opt = a.split("=", 1)[0]
            i += 2 if ("=" not in a and opt in _OPTS_WITH_VALUE) else 1
            continue
        low = a.lower()
        if low in known:
            subs.append(low)
        elif not subs:
            # 认不出的首个裸参数：仍当成子命令（未知子命令不可能是输入类）
            subs.append(low)
        i += 1
    return subs


def _invocations(cmd: str) -> list[tuple[str, list[str], list[str]]]:
    """解析命令里真正会执行的输入注入调用，返回 [(工具名, 子命令列表, 原始参数), ...]

    只认「段首」的注入工具：`echo 'xdotool click 1'` 只是打印一段字符串，
    不点屏幕也不敲键盘，不该为它压遮罩。
    """
    found: list[tuple[str, list[str], list[str]]] = []
    for seg in _segments(cmd):
        toks = _strip_prefixes(_tokens(seg))
        if not toks:
            continue
        tool = os.path.basename(toks[0])
        if tool not in _INPUT_TOOLS:
            continue
        args = toks[1:]
        if tool == "wtype":
            # wtype 没有子命令，用法就是 wtype 后直接跟要打的字
            found.append((tool, ["type"], args))
            continue
        found.append((tool, _parse_subs(tool, args), args))
    return found


def _input_invoked(cmd: str) -> bool:
    """这条命令会不会真的动光标或敲键盘"""
    for tool, subs, _args in _invocations(cmd):
        allowed = _KDOTOOL_INPUT_SUBS if tool == "kdotool" else _XDOTOOL_INPUT_SUBS
        if any(s in allowed for s in subs):
            return True
    return False


def _pointer_click(cmd: str) -> bool:
    """这条命令会不会按下鼠标左/中/右键"""
    for _tool, subs, _args in _invocations(cmd):
        if any(s in _POINTER_CLICK_SUBS for s in subs):
            return True
    return False


def needs_overlay(name: str, args: dict) -> bool:
    """这次工具调用会不会动光标/敲键盘。只有会动的才压遮罩。

    run_command 要按命令内容判断：ls、git status、ps、截图、搜索这类纯后台
    动作一律不弹；只有真正注入鼠标键盘（xdotool/ydotool/wtype/kdotool）才弹。
    """
    if name == "desktop":
        return str(args.get("action", "")) in DESKTOP_ACTION_TOOLS
    if name == "run_command":
        return _input_invoked(str(args.get("command", "")))
    return False

# 智能审批（auto）模式下的危险命令特征：命中则需要用户确认
DANGER_PATTERNS = [
    r"\bsudo\b", r"\brm\b", r"\bmv\b", r"\bdd\b", r"\bmkfs", r"\bshred\b",
    r"\bpacman\b", r"\bflatpak\s+(install|uninstall|remove)",
    r"\bsystemctl\s+(stop|disable|mask)", r"\breboot\b", r"\bshutdown\b",
    r"\bpoweroff\b", r"\bchmod\b", r"\bchown\b", r"\bkill\b", r"\bkillall\b",
    r"\bpkill\b", r"\bsteamos-", r"\bbtrfs\b", r"\bmount\b", r"\bumount\b",
    r"\bfdisk\b", r"\bcurl\b[^|]*\|\s*(ba|z)?sh", r"\bwget\b[^|]*\|\s*(ba|z)?sh",
    r">\s*/(etc|usr|boot|sys|proc)/", r"\bsteam\b[^|]*\binstall",
]


def is_dangerous(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in DANGER_PATTERNS)


# 从命令中识别会写入/改动的文件路径（用于 UI 展示）
WRITE_PATTERNS = [
    r"(?<![\d&])>+\s*([^\s|;&>]+)",                          # > file / >> file（排除 2>&1 这类）
    r"\btee\s+(?:-a\s+)?([^\s|;&]+)",                        # tee file
    r"\bsed\s+-i\S*\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s+([^\s|;&]+)",  # sed -i ... file
    r"\btouch\s+([^\s|;&]+)",
    r"\bmkdir\s+(?:-p\s+)?([^\s|;&]+)",
    r"\bcp\s+(?:-\S+\s+)*[^\s|;&]+\s+([^\s|;&]+)",           # cp src dst
    r"\bmv\s+(?:-\S+\s+)*[^\s|;&]+\s+([^\s|;&]+)",           # mv src dst
    r"\bln\s+(?:-\S+\s+)*[^\s|;&]+\s+([^\s|;&]+)",           # ln target link
]


def extract_writes(cmd: str) -> list[str]:
    files = []
    for pat in WRITE_PATTERNS:
        for m in re.finditer(pat, cmd):
            p = m.group(1)
            if p not in files:
                files.append(p)
    return files[:10]


# 判断「这条命令会不会注入鼠标键盘」的逻辑见上方 _invocations / _input_invoked /
# _pointer_click：解析真实子命令，而不是拿关键词在整串命令里模糊匹配

# 由 main 注入：切换遮罩的鼠标穿透状态 / 截图时收起遮罩
ON_OVERLAY_PASSTHROUGH = None
ON_OVERLAY_LIFT = None

# 判断「这条命令是在拉起一个常驻程序」：浏览器、播放器、游戏这类进程不会自己退出，
# 傻等只会一直卡到超时。单纯启动器（单条命令、无管道/重定向/后台符）且首个程序命中下表，
# 就只等几秒拿到启动输出即可
_LAUNCHER_RE = re.compile(
    r"^\s*(?:env\s+)?(?:\S*/)?(?:"
    r"flatpak|snap|steam|chromium|chromium-browser|google-chrome|google-chrome-stable|"
    r"firefox|mpv|vlc|gedit|kate|dolphin|konsole|steam-rom-manager|retroarch|"
    r"xdg-open|gio|kdesu|prime-run|gamemoderun"
    r")\b")
# 出现这些说明用户/agent 需要命令的真实结果，不能提前返回
_LAUNCHER_HOLD_RE = re.compile(r"[|;&><]|`|\$\(")


def _is_launcher(cmd: str) -> bool:
    if _LAUNCHER_HOLD_RE.search(cmd.replace("2>&1", "")):
        return False
    return bool(_LAUNCHER_RE.match(cmd))


async def _set_overlay_passthrough(on: bool):
    """通知后端：遮罩是否暂时不接鼠标事件（避免 agent 的点击被遮罩当成用户终止）"""
    if ON_OVERLAY_PASSTHROUGH:
        await ON_OVERLAY_PASSTHROUGH(on)


async def _set_overlay_lift(on: bool):
    """通知后端：截图时先把遮罩收起来，免得挡住要拍的画面"""
    if ON_OVERLAY_LIFT:
        await ON_OVERLAY_LIFT(on)


def _fix_display_env(env: dict):
    """补全桌面会话环境变量：X11/Wayland(Xwayland) 授权文件路径每次开机随机"""
    env.setdefault("DISPLAY", ":0")
    if not env.get("XAUTHORITY"):
        hits = glob.glob("/run/user/1000/xauth_*") or glob.glob("/run/user/1000/.mutter-Xwaylandauth*")
        if hits:
            env["XAUTHORITY"] = hits[0]
        elif (Path.home() / ".Xauthority").exists():
            env["XAUTHORITY"] = str(Path.home() / ".Xauthority")
    # ydotool 走 ydotoold 的 socket；服务环境里不一定有，给个默认值
    env.setdefault("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")
    # kdotool 靠这个变量判断是 Plasma 6；缺了会误报「只支持 KDE Plasma 6」
    env.setdefault("KDE_SESSION_VERSION", "6")


# 正在跑的 run_command 子进程。取消靠 asyncio 的 CancelledError 传播，
# 但只要有一处 await 被卡住，那一条取消链就断了；这里留一份登记表，
# 让「终止」还能沿着它把进程树直接砍掉（不依赖任务本身是否响应取消）
_LIVE_PROCS: set[asyncio.subprocess.Process] = set()


async def abort_all_procs(grace: float = 2.0) -> int:
    """杀掉所有登记在案的子进程树，返回处理条数（终止按钮的第二道保险）"""
    procs = list(_LIVE_PROCS)
    for p in procs:
        try:
            await _kill_tree(p, grace)
        except Exception:
            pass
        _LIVE_PROCS.discard(p)
    return len(procs)


async def _kill_tree(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    """杀掉整棵进程树，绝不能只 kill 直接子进程。

    这里踩过一个很隐蔽的坑：`run_command` 用 `bash -c` 跑命令，`proc.kill()`
    只杀掉 bash 本身，孙子进程（`sleep`、`cat`、`spectacle` 之类）还活着并继续
    占着继承来的 stdout 管道。于是随后的 `await proc.wait()` 永远等不到管道 EOF，
    用户点了「终止」之后这一轮就卡在 finally 里出不来 —— 表现就是：
    网页上点了终止，界面还一直显示「正在执行」，遮罩也不撤，必须刷新页面。

    修法：子进程是用 start_new_session=True 起的，本身就是新会话首进程，
    所以 os.killpg 能一次带走整组。先 SIGTERM 让进程有机会自己收尾，
    超时再 SIGKILL 兜底。
    """
    if proc.returncode is not None:
        return
    pgid = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            # 管道被孙进程占着时 wait() 可能一直不返回，所以这里必须带超时；
            # 超时说明还有进程活着，交给下一轮 SIGKILL
            await asyncio.wait_for(proc.wait(), grace)
            return
        except asyncio.TimeoutError:
            continue
    # 走到这里说明进程组已不属于我们（或已退出但 wait 没回来），兜底回收一次
    try:
        await asyncio.wait_for(proc.wait(), 1.0)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _run_command(command: str, reason: str = "", timeout: int = 60, stdin: str = "") -> str:
    timeout = max(1, min(int(timeout or 60), 300))
    env = dict(os.environ)
    _fix_display_env(env)
    # 命令里有鼠标点击时，遮罩必须暂时不接事件，否则这点会点在遮罩上变成「用户终止」；
    # 打开后要等遮罩真正让开，否则点击仍会打在还没隐藏的遮罩上
    await _set_overlay_passthrough(_pointer_click(command))
    feed = None
    if stdin:
        # 用 script 包一层伪终端：/dev/tty 读取和交互菜单都能收到 stdin 输入
        command = f"script -qefc {shlex.quote(command)} /dev/null"
        feed = (stdin if stdin.endswith("\n") else stdin + "\n").encode()
    # 命令里的程序一启动就记录进程组号；进程自己退出后组号不会复用，事后可据此判断「到底起没起来」
    group_file = None
    if _is_launcher(command) and not stdin:
        tmp = tempfile.NamedTemporaryFile(prefix="deckmeido_pgid_", delete=False)
        tmp.close()
        group_file = Path(tmp.name)
        command = f"echo $$ > {shlex.quote(str(group_file))}; {command}"
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE if feed else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(Path.home()),
        env=env,
        executable="/bin/bash",
        start_new_session=True,
    )
    _LIVE_PROCS.add(proc)
    launcher = group_file is not None
    launch_wait = 3.0
    # 真·超时（命令卡死）要杀掉；启动器等满 launch_wait 只是「它在常驻运行」，不能杀
    try:
        if launcher:
            # 常驻程序不会退出：等几秒看它是正常启动还是报错，能拿到输出就先交差
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), launch_wait)
                launcher_done = True
            except asyncio.TimeoutError:
                launcher_done = False
                out = b""
        else:
            launcher_done = True
            out, _ = await asyncio.wait_for(proc.communicate(feed), timeout)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        return (f"命令超时（{timeout}s），已强制终止。\n"
                "提示：若命令需要多次交互输入，请在 stdin 里按行准备好全部答案；"
                "若是长任务，请用 nohup ... > /tmp/日志 2>&1 & 放后台")
    except asyncio.CancelledError:
        # 用户点了终止：整棵进程树一起杀，并向上传递取消。
        # 注意这里不能因为清理失败而吞掉 CancelledError，否则外层会以为「正常结束」
        try:
            await _kill_tree(proc)
        finally:
            raise
    finally:
        # 点击已经发完，遮罩恢复接收点击，用户仍可随时点一下终止
        await _set_overlay_passthrough(False)
        # 这一轮的 await 链已经走完，从登记表里摘掉：
        # 不摘的话「终止」的二次保险会去砍已经交差的、甚至用户故意留着常驻的程序
        _LIVE_PROCS.discard(proc)
    text = out.decode("utf-8", "replace").strip()
    if stdin:
        # 伪终端会回显输入，把疑似密码的行打码（短输入如菜单选项保留）
        for line in stdin.splitlines():
            if len(line.strip()) >= 4:
                text = text.replace(line.strip(), "***")
    if len(text) > 4000:
        text = text[:4000] + "\n…（输出过长已截断）"
    if not launcher_done:
        # 进程还在前台跑：确认程序确实起来了，再放它继续跑，不阻塞后续对话
        started = False
        if group_file is not None:
            try:
                pgid = int(group_file.read_text().strip() or 0)
                started = bool(pgid) and Path(f"/proc/{pgid}").exists()
            except Exception:
                pass
            group_file.unlink(missing_ok=True)
        if not started:
            await _kill_tree(proc)
            return ("命令没有正常启动（进程已退出且没有留下运行中的程序）。"
                    f"\n{text}" if text else "命令没有正常启动（进程已退出且没有留下运行中的程序）。")
        msg = f"已在后台启动（等了 {launch_wait:.0f}s 程序仍在前台运行，判定为常驻程序，已放它继续跑）。"
        return f"{msg}\n{text}" if text else msg
    if group_file is not None:
        group_file.unlink(missing_ok=True)
    return f"exit={proc.returncode}\n{text}" if text else f"exit={proc.returncode}（无输出）"

_CONFIRM_WORDS = {"确认", "好", "好的", "执行", "可以", "yes", "y", "ok"}


def is_confirm(text: str) -> bool:
    return text.strip().lower() in _CONFIRM_WORDS


def summarize_tool(name: str, args: dict) -> str:
    """工具调用的简短中文说明（作为界面提示，不含内部函数名和 JSON 括号）"""
    if name == "run_command":
        cmd = str(args.get("command", ""))
        return cmd if len(cmd) <= 120 else cmd[:120] + "…"
    if name == "web_search":
        return f"「{args.get('query', '')}」"
    if name == "get_weather":
        return f"「{args.get('city', '')}」"
    if name == "find_game":
        return f"「{args.get('name', '')}」"
    if name == "install_game":
        return f"「{args.get('appid', '')}」"
    if name == "desktop":
        act = args.get("action", "")
        if act == "focus":
            return f"切到「{args.get('target', '')}」"
        if act == "click":
            return f"点击 ({args.get('x', 0)}, {args.get('y', 0)})"
        if act == "move":
            return f"移动光标到 ({args.get('x', 0)}, {args.get('y', 0)})"
        if act == "type":
            t = str(args.get("text", ""))
            return f"输入「{t[:40]}」"
        return "查看光标与焦点"
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        path = str(args.get("path", ""))
        return path.split("/")[-1] or path
    # 无需参数的简单操作：不显示多余说明
    if name in ("screenshot", "list_installed_games", "check_system_updates", "list_emulator_roms"):
        return ""
    if name == "self_update":
        return "查询新版本" if args.get("action") != "upgrade" else "升级并重启服务"
    if not args:
        return ""
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 120 else s[:120] + "…"


def describe(name: str, args: dict) -> str:
    """危险操作的人类可读描述"""
    if name == "install_game":
        return f"下载安装 Steam 游戏（appid: {args.get('appid')}）"
    if name == "self_update":
        return "把 DeckMeido 升级到最新版：拉取新代码并重启服务（当前对话会中断）"
    if name == "run_command":
        return f"执行命令：{args.get('command', '')}\n目的：{args.get('reason', '')}"
    detail = summarize_tool(name, args)
    return f"{cn_name(name)}：{detail}" if detail else cn_name(name)


# ---------- 工具实现 ----------

async def _get_weather(city: str) -> str:
    geo = await HTTP.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "zh", "format": "json"},
    )
    results = geo.json().get("results")
    if not results:
        return f"找不到城市：{city}"
    loc = results[0]
    w = await HTTP.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto",
        },
    )
    cur = w.json()["current"]
    codes = {0: "晴", 1: "大致晴", 2: "局部多云", 3: "阴", 45: "雾", 48: "冻雾",
             51: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 71: "小雪", 73: "中雪",
             75: "大雪", 80: "阵雨", 95: "雷阵雨"}
    desc = codes.get(cur["weather_code"], f"天气代码 {cur['weather_code']}")
    return (
        f"{loc.get('name', city)}（{loc.get('country', '')}）当前：{desc}，"
        f"气温 {cur['temperature_2m']}°C，体感 {cur['apparent_temperature']}°C，"
        f"湿度 {cur['relative_humidity_2m']}%，风速 {cur['wind_speed_10m']} km/h"
    )


async def _web_search(query: str) -> str:
    def _search() -> list[dict]:
        from ddgs import DDGS
        return DDGS().text(query, max_results=5)

    try:
        results = await asyncio.to_thread(_search)
    except Exception as e:
        return f"搜索失败：{e}"
    if not results:
        return "没有搜到相关结果"
    lines = [f"{i+1}. {r.get('title', '')}\n   {r.get('body', '')}\n   {r.get('href', '')}"
             for i, r in enumerate(results)]
    return "\n".join(lines)


def _parse_acf(text: str) -> dict:
    """解析 Valve ACF/VDF 键值格式（浅层）"""
    out = {}
    for m in re.finditer(r'"(\w+)"\s+"([^"]*)"', text):
        out[m.group(1)] = m.group(2)
    return out


async def _list_installed_games() -> str:
    steamapps_dirs = [Path.home() / ".steam/steam/steamapps"]
    lf = steamapps_dirs[0] / "libraryfolders.vdf"
    if lf.exists():
        for m in re.finditer(r'"path"\s+"([^"]+)"', lf.read_text(errors="ignore")):
            d = Path(m.group(1)) / "steamapps"
            if d not in steamapps_dirs:
                steamapps_dirs.append(d)

    games = []
    for d in steamapps_dirs:
        for acf in d.glob("appmanifest_*.acf"):
            info = _parse_acf(acf.read_text(errors="ignore"))
            if info.get("name"):
                size_gb = int(info.get("SizeOnDisk", 0)) / 2**30
                games.append(f"- {info['name']}（appid {info.get('appid')}，{size_gb:.1f} GB）")
    if not games:
        return "没有找到已安装的 Steam 游戏"
    return f"共 {len(games)} 款已安装游戏：\n" + "\n".join(sorted(games))


async def _find_game(name: str) -> str:
    r = await HTTP.get(
        "https://store.steampowered.com/api/storesearch/",
        params={"term": name, "l": "schinese", "cc": "cn"},
    )
    items = r.json().get("items", [])[:5]
    if not items:
        return f"Steam 商店没找到：{name}"
    lines = [f"- {it['name']}（appid {it['id']}）" for it in items]
    return "搜索结果：\n" + "\n".join(lines)


async def _self_update(action: str = "check") -> str:
    """检查或升级 DeckMeido 自身（复用 server/updater.py，与设置页的按钮同一套逻辑）"""
    from . import updater

    if action != "upgrade":
        try:
            info = await updater.check()
        except RuntimeError as e:
            return f"检查更新失败：{e}"
        if not info["tag"]:
            return f"当前版本 {info['current']}，GitHub 上还没有发布版本信息"
        if not info["has_update"]:
            return f"当前已是最新版本（{info['current']}）"
        return (f"当前 {info['current']}，最新版本 {info['tag']}"
                + (f"（{info['published_at'][:10]}）" if info.get("published_at") else "")
                + "\n需要我升级的话说一声，升级时服务会重启一下。")

    try:
        res = await asyncio.to_thread(updater.apply, True)
    except RuntimeError as e:
        return f"升级失败：{e}"
    return f"{res['message']}。日志：{res.get('log', '')}"


async def _check_system_updates() -> str:
    proc = await asyncio.create_subprocess_exec(
        "steamos-update", "check",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="ignore").strip() or "检查完成，无输出"


async def _install_game(appid: int) -> str:
    proc = await asyncio.create_subprocess_exec(
        "steam", f"steam://install/{appid}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()
    return f"已向 Steam 发送安装指令（appid {appid}），下载会在 Steam 客户端中开始"


# 不算 ROM 的杂项文件（存档/封面/数据库等）
ROM_EXCLUDE_EXT = {
    ".txt", ".xml", ".md", ".json", ".dat", ".db", ".png", ".jpg", ".jpeg",
    ".webp", ".cue", ".m3u", ".srm", ".sav", ".state", ".dst", ".dsv",
    ".cfg", ".ini", ".log", ".bak", ".tmp", ".bin.index",
}
# 在 Games 等非标准目录里，只认这些模拟器游戏扩展名
ROM_INCLUDE_EXT = {
    ".nsp", ".xci", ".nro", ".iso", ".gcm", ".rvz", ".wbfs", ".chd",
    ".gba", ".gbc", ".gb", ".nds", ".3ds", ".cia", ".nes", ".sfc", ".smc",
    ".md", ".sms", ".n64", ".z64", ".v64", ".pbp", ".cso", ".wad",
}
# 不计入游戏数的部分（DLC/升级补丁）
ROM_SKIP_DIRS = {"dlc", "dlcs", "update", "updates", "upd", "runtime"}
ROM_SKIP_PREFIX = ("[dlc]", "[upd]", "[update]")


def _count_roms(directory: Path, by_ext_only: bool) -> int:
    n = 0
    for f in directory.rglob("*"):
        if not f.is_file() or f.name.startswith("."):
            continue
        # 跳过 DLC/补丁目录与文件
        if any(part.lower() in ROM_SKIP_DIRS for part in f.parts):
            continue
        if f.name.lower().startswith(ROM_SKIP_PREFIX):
            continue
        ext = f.suffix.lower()
        if by_ext_only:
            if ext in ROM_INCLUDE_EXT:
                n += 1
        elif ext not in ROM_EXCLUDE_EXT:
            n += 1
    return n


async def _list_emulator_roms(system: str = "") -> str:
    def _scan() -> dict:
        counts: dict[str, int] = {}

        # EmuDeck 标准结构：<root>/<机型>/ 下所有非杂项文件都算 ROM
        roots = [Path.home() / "Emulation/roms", Path.home() / "ROMs", Path.home() / "roms"]
        media = Path("/run/media")
        if media.is_dir():
            for card in media.iterdir():
                if card.is_dir():
                    roots.append(card / "Emulation/roms")
        for root in roots:
            if not root.is_dir():
                continue
            for sub in sorted(root.iterdir()):
                if not sub.is_dir():
                    continue
                n = _count_roms(sub, by_ext_only=False)
                if n:
                    counts[sub.name] = counts.get(sub.name, 0) + n

        # SD 卡 Games 目录：<card>/Games/<合集名>/ 下按扩展名识别
        if media.is_dir():
            for card in media.iterdir():
                games = card / "Games"
                if not games.is_dir():
                    continue
                for sub in sorted(games.iterdir()):
                    if not sub.is_dir() or sub.name.lower() in ROM_SKIP_DIRS:
                        continue
                    n = _count_roms(sub, by_ext_only=True)
                    if n:
                        counts[sub.name] = counts.get(sub.name, 0) + n
        return counts

    counts = await asyncio.to_thread(_scan)
    if system:
        counts = {k: v for k, v in counts.items() if system.lower() in k.lower()}
        if not counts:
            return f"没有找到 {system} 相关的游戏目录"
    if not counts:
        return "没有找到模拟器 ROM（已扫描 ~/Emulation/roms、SD 卡 Games 等目录）"
    lines = [f"- {k}: {v} 个" for k, v in sorted(counts.items())]
    return f"共 {sum(counts.values())} 个游戏文件：\n" + "\n".join(lines)


async def _gamescope_pw_node() -> str | None:
    """游戏模式下找 gamescope 的 PipeWire 视频节点 id；找不到返回 None"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        nodes = json.loads(out or b"[]")
        for o in nodes:
            props = (o.get("info") or {}).get("props") or {}
            if props.get("node.name") == "gamescope" and props.get("media.class") == "Video/Source":
                return str(o.get("id"))
    except Exception:
        return None
    return None


async def _screenshot_gamescope(path: Path) -> bool:
    """游戏模式：从 gamescope 的 PipeWire 视频节点直接抓一帧（不经过桌面会话）"""
    node = await _gamescope_pw_node()
    if not node or not shutil.which("gst-launch-1.0"):
        return False
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    raw = path.with_suffix(".raw.png")
    cmd = [
        "gst-launch-1.0", "-q", f"pipewiresrc path={node} num-buffers=6", "!",
        "videoconvert", "!", "pngenc", "!", f"filesink location={raw}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env,
        )
        await asyncio.wait_for(proc.wait(), timeout=25)
        if not raw.exists() or raw.stat().st_size < 10000:
            return False
        # pngenc 连续 6 帧会覆盖同一文件，取到的就是最后一帧，直接搬过来
        raw.replace(path)
        return True
    except Exception:
        return False
    finally:
        raw.unlink(missing_ok=True)


async def _screenshot() -> str:
    """截取当前画面（游戏模式走 gamescope 的 PipeWire 节点，桌面模式用 spectacle/mss）"""
    import time

    shots_dir = Path(__file__).resolve().parent / "static" / "shots"
    shots_dir.mkdir(exist_ok=True)
    # 只保留最近 20 张截图
    old = sorted(shots_dir.glob("shot_*.png"), key=lambda p: p.stat().st_mtime)
    for p in old[:-20]:
        p.unlink(missing_ok=True)
    name = f"shot_{int(time.time())}.png"
    path = shots_dir / name

    # 游戏模式：桌面会话已经结束，只能从 gamescope 抓
    if await _gamescope_pw_node():
        if await _screenshot_gamescope(path):
            return await _finish_shot(path, name, shots_dir, time)
        return "截图失败：检测到游戏模式，但从 gamescope 的 PipeWire 节点抓帧失败"

    env = dict(os.environ)
    _fix_display_env(env)
    # 遮罩会盖住整个桌面，先把它收起来，截完再放回来
    await _set_overlay_lift(True)
    try:
        if shutil.which("spectacle"):
            proc = await asyncio.create_subprocess_exec(
                "spectacle", "-bno", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
            )
            await proc.wait()
        if not path.exists() or path.stat().st_size < 10000:
            # spectacle 不可用或截到黑屏（Xwayland rootless）时退回 mss
            import mss
            with mss.mss() as sct:
                sct.shot(output=str(path))
    finally:
        await _set_overlay_lift(False)
    if not path.exists():
        return "截图失败：spectacle 与 mss 都不可用"
    return await _finish_shot(path, name, shots_dir, time)


async def _finish_shot(path: Path, name: str, shots_dir: Path, time_mod) -> str:
    """压缩一份给多模态模型看（1280 宽 JPEG），并返回给模型的结果文本"""
    global LAST_VISION
    LAST_VISION = None
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((1280, 1280))
        vpath = shots_dir / f"vision_{int(time_mod.time())}.jpg"
        img.convert("RGB").save(vpath, "JPEG", quality=70)
        LAST_VISION = vpath
    except Exception:
        pass
    return f"截图完成：/shots/{name}。当前画面已附上，你可以直接根据画面内容回答用户的问题。"


async def _kdotool(*args: str) -> tuple[int, str]:
    """跑一条 kdotool 命令，返回 (退出码, 输出)"""
    env = dict(os.environ)
    _fix_display_env(env)
    env["KDE_SESSION_VERSION"] = "6"  # kdotool 靠它判断 Plasma 6
    proc = await asyncio.create_subprocess_exec(
        "kdotool", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    out, err = await proc.communicate()
    return proc.returncode, (out + err).decode(errors="replace").strip()


async def _ydotool(*args: str) -> str:
    """跑一条 ydotool 命令，返回输出（失败时抛错）"""
    env = dict(os.environ)
    _fix_display_env(env)
    proc = await asyncio.create_subprocess_exec(
        "ydotool", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    out, err = await proc.communicate()
    text = (out + err).decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(text or f"ydotool {args[0]} 失败")
    return text


async def _cursor() -> tuple[int, int]:
    code, text = await _kdotool("getmouselocation")
    if code != 0:
        raise RuntimeError(f"读光标位置失败：{text}")
    mx = re.search(r"x:(-?\d+)", text)
    my = re.search(r"y:(-?\d+)", text)
    return (int(mx.group(1)) if mx else 0, int(my.group(1)) if my else 0)


async def _move_to(x: int, y: int) -> None:
    """移到屏幕绝对坐标。ydotoold 的虚拟设备没有绝对坐标轴，
    mousemove --absolute 会静默失效，所以必须读当前位置算相对位移"""
    cx, cy = await _cursor()
    dx, dy = x - cx, y - cy
    if dx or dy:
        await _ydotool("mousemove", "--", str(dx), str(dy))


async def _desktop(action: str, target: str = "", x: int = 0, y: int = 0, text: str = "") -> str:
    """桌面窗口操作：切焦点 / 点击 / 移动 / 打字。命中点击时自动打开遮罩穿透"""
    if action == "focus":
        if not target:
            return "focus 需要提供 target（窗口标题关键词）"
        code, out = await _kdotool("search", "--name", target)
        if code != 0 or not out:
            return f"没找到标题匹配「{target}」的窗口"
        wid = out.splitlines()[0].strip()
        await _kdotool("windowactivate", wid)
        await _kdotool("windowraise", wid)
        _, name = await _kdotool("getwindowname", wid)
        return f"已切到前台：{name}"

    if action in ("move", "click"):
        # 遮罩盖在桌面最上层：不打开穿透，光标会被挡在遮罩下面（移动也看不见落点），
        # 点击更会被当成「用户终止」，所以 move 也要先让开
        await _set_overlay_passthrough(True)
        try:
            await _move_to(x, y)
            if action == "click":
                await asyncio.sleep(0.25)
                await _ydotool("click", "0xC0")
                cx, cy = await _cursor()
                _, focus = await _kdotool("getactivewindow", "getwindowname")
                return f"已点击 ({cx}, {cy})，当前焦点窗口：{focus}"
            cx, cy = await _cursor()
            return f"光标已移到 ({cx}, {cy})"
        finally:
            await _set_overlay_passthrough(False)

    if action == "type":
        if not text:
            return "type 需要提供 text"
        # ydotool type 不支持中文；输入前若焦点不在目标窗口，调用方应先 focus
        await _ydotool("type", "--", text)
        return f"已输入 {len(text)} 个字符"

    if action == "where":
        cx, cy = await _cursor()
        _, focus = await _kdotool("getactivewindow", "getwindowname")
        return f"光标：({cx}, {cy})\n焦点窗口：{focus}"

    return f"不支持的动作：{action}"


# 最近一次截图的模型可读版本路径（供 run_agent 注入多模态消息）
LAST_VISION: Path | None = None

# 最近一次文件写入/编辑的增删行数（供 UI 展示 diff 统计）
LAST_DIFF: dict | None = None


WORKSPACE = Path.home() / "deckmeido_workspace"


def set_workspace(path: str):
    """切换工作区目录（设置面板可改），目录不存在则创建"""
    global WORKSPACE
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    WORKSPACE = p.resolve()


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


async def _list_dir(path: str = "") -> str:
    d = _resolve(path) if path else WORKSPACE
    d.mkdir(parents=True, exist_ok=True) if not path else None
    if not d.is_dir():
        return f"不是目录：{d}"
    entries = []
    for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
        tag = "[目录]" if p.is_dir() else "[文件]"
        size = f" ({p.stat().st_size}B)" if p.is_file() else ""
        entries.append(f"{tag} {p.name}{size}")
    return f"{d} 共 {len(entries)} 项：\n" + ("\n".join(entries[:200]) or "（空）")


async def _read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"文件不存在：{p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    offset = max(1, offset)
    chunk = lines[offset - 1: offset - 1 + min(limit, 2000)]
    body = "\n".join(f"{offset + i}→{line}" for i, line in enumerate(chunk))
    note = f"（共 {len(lines)} 行，显示 {offset}-{offset - 1 + len(chunk)}）"
    return f"{p} {note}\n{body}"


async def _write_file(path: str, content: str) -> str:
    global LAST_DIFF
    LAST_DIFF = None
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    old_lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines()) if p.is_file() else 0
    p.write_text(content, encoding="utf-8")
    LAST_DIFF = {"add": len(content.splitlines()), "del": old_lines}
    return f"已写入 {p}（{len(content)} 字符）"


async def _edit_file(path: str, old: str, new: str) -> str:
    global LAST_DIFF
    LAST_DIFF = None
    p = _resolve(path)
    if not p.is_file():
        return f"文件不存在：{p}"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "替换失败：old 文本在文件中不存在（先用 read_file 确认原文）"
    if count > 1:
        return f"替换失败：old 文本出现了 {count} 次，请扩大范围使其唯一"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    LAST_DIFF = {"add": len(new.splitlines()), "del": len(old.splitlines())}
    return f"已修改 {p}（替换 1 处）"


EXECUTORS = {
    "get_weather": _get_weather,
    "web_search": _web_search,
    "list_installed_games": _list_installed_games,
    "find_game": _find_game,
    "check_system_updates": _check_system_updates,
    "install_game": _install_game,
    "list_emulator_roms": _list_emulator_roms,
    "run_command": _run_command,
    "desktop": _desktop,
    "screenshot": _screenshot,
    "list_dir": _list_dir,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "self_update": _self_update,
}


async def execute(name: str, args: dict) -> str:
    fn = EXECUTORS.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return await fn(**args)
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"
