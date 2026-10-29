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
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from . import power

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
    "gamepad": "操作手柄",
    "screenshot": "截取屏幕",
    "list_dir": "查看目录",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "self_update": "更新软件",
    "remember": "记住这件事",
    "ask_user": "向你提问",
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
                            "click 在屏幕坐标处左键单击；move 移动光标；"
                            "scroll 在 (x,y) 处滚动（dx 正数向右、dy 正数向下，一格约 120）；"
                            "drag 从 (x,y) 按住左键拖到 (x2,y2)，中间自动插值，可用来拖滑块/拖文件/拖选中；"
                            "swipe 模拟触摸板滑动（dx/dy 填屏幕像素，内部按步换算成滚轮格数）——"
                            "steam 界面、浏览器等横向翻页用它，纵向滚动用 scroll 更精确；"
                            "type 在当前窗口打字（中英文都支持）；"
                            "where 查光标和焦点窗口。"
                            "坐标从 screenshot 画面上读，左上角为 0,0。"
                            "找不到窗口时先怀疑程序缩进了系统托盘（托盘项 kdotool 搜不到也激活不了），"
                            "进程还在就结束进程重新启动，主窗口才会回来"),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["focus", "click", "move", "scroll", "drag", "swipe", "type", "where"],
                               "description": "要做的动作"},
                    "target": {"type": "string", "description": "focus 时填窗口标题关键词（如 Kazumi）"},
                    "x": {"type": "integer", "description": "click/move/scroll 的起点、drag/swipe 的起点的屏幕横坐标"},
                    "y": {"type": "integer", "description": "click/move/scroll 的起点、drag/swipe 的起点的屏幕纵坐标"},
                    "x2": {"type": "integer", "description": "drag 的终点横坐标"},
                    "y2": {"type": "integer", "description": "drag 的终点纵坐标"},
                    "dx": {"type": "integer", "description": "scroll 的横向格数（正右负左）；swipe 的横向像素（正右负左）"},
                    "dy": {"type": "integer", "description": "scroll 的纵向格数（正下负上）；swipe 的纵向像素（正下负上）"},
                    "text": {"type": "string", "description": "type 时要输入的文本，中英文均可（走 KDE 剪贴板粘贴）"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            # 只把问题交给界面，不执行任何东西。给了 options 就在对话框里摆成
            # 可点的按钮（像 IDE 里的选择支），用户点一下就是回答；不给就是
            # 开放提问，等用户自己打字
            "description": ("向用户提问，让对话里出现选项按钮或问题卡片。"
                            "信息不足、需要用户在几个方案里挑一个、或要确认偏好时用它，"
                            "比在正文里列 1/2/3 更清楚。给了 options 就渲染成可点的按钮，"
                            "用户点的那一项会作为他的回答发回来；"
                            "不给 options 就是单纯把问题抛出来，等用户自己输入。"
                            "调用后这一轮就结束，不要自己替用户选"),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问用户的问题，一句话说清"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选项（最多 6 个，每个一行短句）。不需要选项就不传",
                    },
                },
                "required": ["question"],
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
            "name": "gamepad",
            "description": ("模拟手柄按键（写 /dev/uinput 虚拟手柄，游戏模式里唯一能被游戏认到的注入方式；"
                            "desktop 那套鼠标键盘在游戏里没用）。"
                            "action=press 按一下某个键；hold 按住某个键若干毫秒（跑动这类需要长按）；"
                            "stick 把摇杆推到 (x,y) 并保持；trigger 把扳机压到 value 力度；"
                            "release 摇杆回中并松开扳机。"
                            "玩游戏、过对话、菜单导航时用。一次只能动一个键，"
                            "需要连续操作就多次调用"),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["press", "hold", "stick", "trigger", "release"],
                               "description": "要做的动作"},
                    "button": {"type": "string",
                               "enum": ["a", "b", "x", "y", "up", "down", "left", "right",
                                        "lb", "rb", "lt", "rt", "start", "select", "guide",
                                        "l3", "r3", "share"],
                               "description": ("press/hold/trigger 的键位。a 通常是确认/跳跃，"
                                               "b 取消，x/y 视游戏而定；方向键是十字键不是摇杆，"
                                               "需要模拟摇杆用 stick。"
                                               "lt/rt 是左右扳机（0..1 力度，press/hold 时按到底），"
                                               "l3/r3 是左右摇杆按下（跑步/蹲/近战常见）。"
                                               "start 是 Xbox 的 Menu 键（≡），select 是 View 键（◱），"
                                               "两者在游戏里常被用来打开暂停菜单/地图；"
                                               "guide 是 Xbox 的 Xbox/Guide 键，游戏内可呼出 overlay；"
                                               "share 是 Xbox Series 的分享键，部分游戏不认；"
                                               "guide 不是 Steam 键，无法呼出 Steam 菜单，需要那个请让用户自己按")},
                    "ms": {"type": "integer",
                           "description": "hold 按住多久（毫秒，默认 500，上限 5000）；press 默认 80"},
                    "x": {"type": "number",
                          "description": "stick 的横向，-1 最左、1 最右，0 居中"},
                    "y": {"type": "number",
                          "description": "stick 的纵向，-1 最上、1 最下，0 居中"},
                    "side": {"type": "string",
                             "enum": ["left", "right"],
                             "description": "stick/release 用哪个摇杆，默认 left（右摇杆主要用来转视角）"},
                    "value": {"type": "number",
                              "description": "trigger 的力度，0 松开、1 按到底，默认 1"},
                },
                "required": ["action"],
            },
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
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": ("把一条信息存入长期记忆（跨会话有效，新会话也会带上）。"
                            "用户明确说「记住」「以后都这样」或纠正你的做法时调用；"
                            "只顾当前这句话的临时要求不要存。一条只记一件事，"
                            "用陈述句写成以后也看得懂的一句话"),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记住的内容，一句话"},
                    "kind": {"type": "string", "enum": ["profile", "env", "note"],
                             "description": ("profile=主人的偏好与习惯（回答风格、称呼、禁忌）；"
                                             "env=这台设备上的环境与操作经验（怎么装、怎么启动、"
                                             "某个路径在哪）；note=项目或知识条目。默认 note")},
                },
                "required": ["text"],
            },
        },
    },
]

# 危险操作：任何权限模式下都需确认
# gamepad 写的是 /dev/uinput 虚拟手柄，按下去会直接操作当前游戏/前台界面，
# 用户不在旁边看着的时候不该自己动 —— 归到必确认，和装游戏同一级别
DANGEROUS = {"install_game", "gamepad"}


def touches_screen(name: str, args: dict) -> bool:
    """这次工具调用会不会真的动屏幕（注入鼠标/键盘、改焦点、翻窗口）。

    遮罩只该盖在「手没法去点终止」的时候；跑命令、翻文件、查天气这类
    纯后台动作不挡视线。desktop 的 where 只读光标和焦点，不动屏幕。
    """
    if name == "desktop":
        return str(args.get("action", "")) != "where"
    # 手柄注入也是「真的在操作游戏」：按下去游戏就在动，遮罩该盖就盖
    if name == "gamepad":
        return True
    if name == "run_command":
        return bool(_screen_command(str(args.get("command", ""))))
    return False


def pointer_action(name: str, args: dict) -> bool:
    """这次调用会不会按下鼠标键（点击 / 拖拽 / 滚轮）。

    这类动作执行期间遮罩必须整块让开：遮罩是 XWayland 窗口，Wayland 合成器
    做命中测试时看不见 X11 的 input shape，窗口还在就一定会把点击吃掉。
    """
    if name == "desktop":
        return str(args.get("action", "")) in _POINTER_ACTIONS
    if name == "run_command":
        return _pointer_click(str(args.get("command", "")))
    return False


# desktop 里会按下鼠标键的动作（move 只挪光标，不按键）
_POINTER_ACTIONS = {"click", "scroll", "drag", "swipe"}


# run_command 里能干出「动屏幕」这件事的命令：输入注入、窗口切换、弹出可见窗口/通知
_SCREEN_CMDS = ("xdotool", "ydotool", "dotool", "kdotool", "wtype",
                "wmctrl", "notify-send", "zenity", "kdialog")
_SCREEN_LAUNCHERS = ("xdg-open", "steam", "chromium", "google-chrome", "firefox")


def _screen_command(cmd: str) -> list[str]:
    """拆开命令行，找出会动屏幕的那些程序名（只认命令位置上的词）"""
    hits: list[str] = []
    for seg in re.split(r"&&|\|\||[;|\n]", cmd):
        for tok in _shlex_words(seg):
            if re.fullmatch(r"[A-Za-z_]\w*=\S*", tok):
                continue  # 环境变量前缀，跳过
            base = os.path.basename(tok)
            if base in _SCREEN_CMDS or base in _SCREEN_LAUNCHERS:
                hits.append(base)
            break  # 一条子命令只看头一个词
    return hits


def _shlex_words(seg: str) -> list[str]:
    try:
        return shlex.split(seg, comments=False, posix=True)
    except ValueError:
        return seg.split()


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
# 会让光标按下去的子命令：这些必须打开遮罩穿透，否则点会落在遮罩上变成「用户终止」。
# mousemove/move 刻意不在里面：只挪光标不会产生按下事件，遮罩放着不动也点不到它，
# 反而能给用户留一个「随时点一下终止」的入口。以前把 move 也算进来，于是
# 「移动光标 → 点某个按钮」这类操作在移动阶段就让位，遮罩整个变成不接点击，
# 用户以为点了没反应。
_POINTER_CLICK_SUBS = {"click", "mousedown", "mouseup", "drag", "scroll", "wheel"}

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
    """这条命令会不会按下鼠标键（含滚轮；滚轮同样算一次按键注入）"""
    for _tool, subs, _args in _invocations(cmd):
        if any(s in _POINTER_CLICK_SUBS for s in subs):
            return True
    return False

# 智能审批（auto）模式下的危险命令特征：命中则需要用户确认。
# 这些正则只是第一道网，真正能挡住 sh -c / base64 | sh / python -c 这类绕法的是
# 下面的 _structural_danger()：先分词，再递归看每个子命令到底在调什么
DANGER_PATTERNS = [
    r"\bsudo\b", r"\brm\b", r"\bmv\b", r"\bdd\b", r"\bmkfs", r"\bshred\b",
    r"\bpacman\b", r"\bflatpak\s+(install|uninstall|remove)",
    r"\bsystemctl\s+(stop|disable|mask)", r"\breboot\b", r"\bshutdown\b",
    r"\bpoweroff\b", r"\bchmod\b", r"\bchown\b", r"\bkill\b", r"\bkillall\b",
    r"\bpkill\b", r"\bsteamos-", r"\bbtrfs\b", r"\bmount\b", r"\bumount\b",
    r"\bfdisk\b", r"\bcurl\b[^|]*\|\s*(ba|z)?sh", r"\bwget\b[^|]*\|\s*(ba|z)?sh",
    r">\s*/(etc|usr|boot|sys|proc)/", r"\bsteam\b[^|]*\binstall",
]

# 本身就有破坏力的程序名（取 basename 后比较）
_DANGER_PROGS = {
    "sudo", "doas", "rm", "rmdir", "mv", "dd", "shred", "mkfs", "mkswap",
    "fdisk", "parted", "gdisk", "wipefs", "btrfs", "pacman", "yay", "paru",
    "reboot", "shutdown", "poweroff", "halt", "chmod", "chown", "chattr",
    "kill", "killall", "pkill", "mount", "umount", "swapon", "swapoff",
    "systemctl", "steamos-readonly", "flatpak", "steamos-update",
}
# 这些程序后面跟 -c / -e 时，参数本身就是一整条命令，必须递归检查
_SHELL_PROGS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "su"}
# 能把 stdin 变成可执行代码的解码器/解释器
_DECODER_PROGS = {"base64", "xxd", "uudecode", "openssl"}
# 解释器带 -c/-e 时，参数里可能是任意代码
_CODE_PROGS = {"python", "python3", "perl", "ruby", "node", "php"}


def _prog_name(tok: str) -> str:
    """取程序名：去掉路径、引号、反斜杠转义（\\rm -> rm，s""udo 由分词处理）"""
    tok = tok.strip("\"'")
    tok = tok.replace("\\", "")
    return os.path.basename(tok)


def _structural_danger(cmd: str, depth: int = 0) -> bool:
    """按命令结构判断危险性：分词后逐个看子命令与它们的参数

    能挡住纯正则漏掉的写法：
      sh -c 'rm -rf ~' / bash -c "$(echo cm0g)" / echo xxx | base64 -d | sh
      python3 -c 'import os;os.system("rm -rf ~")' / \rm -rf ~ / s""udo ...
    """
    if depth > 3:
        # 嵌套过深（比如多层 eval 拼接）无法可靠分析，按危险处理
        return True
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        # 引号不闭合等解析失败：分析不了就当作危险，交给用户确认
        return True

    # 按 shell 运算符切分子命令：; && || | & 以及 $( ) 反引号包裹的部分
    groups: list[list[str]] = [[]]
    for tok in toks:
        if tok and set(tok) <= set(";&|()`"):
            if tok in ("(", ")") or tok.startswith("$"):
                # 命令替换/子 shell：里面的部分同样要查
                continue
            groups.append([])
            continue
        groups[-1].append(tok)

    for group in groups:
        if not group:
            continue
        # 跳过前置环境变量赋值（FOO=1 cmd）
        i = 0
        while i < len(group) and re.fullmatch(r"[A-Za-z_]\w*=.*", group[i]):
            i += 1
        if i >= len(group):
            continue
        prog = _prog_name(group[i])
        rest = group[i + 1:]
        if prog in _DANGER_PROGS:
            # flatpak 只有 install/uninstall/remove 才算改动系统
            if prog == "flatpak":
                if any(a in ("install", "uninstall", "remove", "update") for a in rest):
                    return True
            elif prog == "systemctl":
                if any(a in ("stop", "disable", "mask", "restart", "kill") for a in rest):
                    return True
            elif prog in ("chmod", "chown") and not any(
                    a.startswith(("-R", "--recursive")) for a in rest):
                # 普通 chmod/chown 未必危险，但改系统目录的一律拦下
                if any(a.startswith(("/etc", "/usr", "/boot", "/sys", "/proc"))
                       for a in rest):
                    return True
            else:
                return True
        # 解释器/解码器：把参数交给下一层继续查
        if prog in _SHELL_PROGS:
            return True
        if prog in _CODE_PROGS:
            has_code = any(a == "-c" or a == "-e" or a.startswith("--eval") for a in rest)
            if has_code:
                return True
        if prog in _DECODER_PROGS:
            # base64 -d / openssl base64 -d：解码出来的东西一旦接管道执行就已经拆分了，
            # 解码器本身不危险，但配合管道执行在下面 _pipe_to_shell 里单独判
            continue
    # 管道喂给 shell：xxx | sh / | bash / | source
    if re.search(r"\|\s*(?:sudo\s+)?(?:ba|da|z|k)?sh\b", cmd) or \
            re.search(r"\|\s*(?:sudo\s+)?(?:python3?|perl|ruby|node)\b", cmd):
        return True
    # 重定向覆盖系统目录
    if re.search(r">\s*/(etc|usr|boot|sys|proc|bin|sbin|lib)/", cmd):
        return True
    # 命令替换里跑了危险程序：$(rm -rf ...)
    for m in re.finditer(r"\$\(([^()]*)\)", cmd):
        if _structural_danger(m.group(1), depth + 1):
            return True
    # 反引号同理
    for m in re.finditer(r"`([^`]*)`", cmd):
        if _structural_danger(m.group(1), depth + 1):
            return True
    return False


def is_dangerous(cmd: str) -> bool:
    # 归一化：去掉零宽字符与多余空白，避免 "r m" 之外的花招干扰分词
    norm = re.sub(r"[\u200b-\u200f\u2028\u2029]", "", cmd or "")
    if any(re.search(p, norm) for p in DANGER_PATTERNS):
        return True
    return _structural_danger(norm)


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
# 前面允许挂 cd / 环境变量赋值这类不改变「这是在启动程序」实质的前缀：
# 「cd ~/x && flatpak run ...」是最常见的写法，以前因为含 && 被判成普通命令，
# 于是傻等 60 秒超时，还把用户要的程序一起杀掉
_LAUNCHER_PREFIX_RE = re.compile(
    r"^\s*(?:(?:export\s+\w+=[^\s;]*|cd\s+[^;&|]+|[A-Za-z_]\w*=[^\s;]*)\s*(?:&&|;)\s*)*")
# env/setsid/nohup 这类包装器不改变实质，一并剥掉（含内联的 FOO=1 env 形式）
_LAUNCHER_WRAP_RE = re.compile(r"^(?:(?:env|setsid|nohup|[A-Za-z_]\w*=\S*)\s+)+")
# 出现这些说明命令里还串着别的步骤，需要真实结果，不能提前返回。
# 前缀（cd/赋值 + &&）已经在 _is_launcher 里剥掉了，这里剩下的分隔符
# 都是「同一条命令里还塞了别的事」
_LAUNCHER_HOLD_RE = re.compile(r"[|;<>`]|\$\(|&&")


def _is_launcher(cmd: str) -> bool:
    # 先剥掉 cd / 赋值这类前缀，再看剩下的命令是不是单纯启动一个程序
    body = _LAUNCHER_PREFIX_RE.sub("", cmd, count=1)
    # 末尾的 & 只是「放后台」，不影响判据
    body = body.rstrip().rstrip("&").rstrip()
    # 输出重定向不改变「这是在启动程序」的事实，先摘掉（含 2>&1 这种 fd 复制）；
    # 用 <（读输入）或管道/分号串了新命令的则保留，交给下面的判断
    body = re.sub(r"\d*>>?\s*(?:&\s*\d+|[^\s;&|]+)", "", body).strip()
    body = _LAUNCHER_WRAP_RE.sub("", body, count=1)
    if _LAUNCHER_HOLD_RE.search(body):
        return False
    return bool(_LAUNCHER_RE.match(body))


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
# 命令超时转后台后，负责把它的输出抄进日志的任务；留着引用防止被 GC 回收
_BG_DRAINERS: set[asyncio.Task] = set()
# 已转后台的常驻进程：不在 _LIVE_PROCS 里（那轮 await 已经走了），
# 但用户点「终止」时仍然应该能被收掉
_BG_PROCS: set[asyncio.subprocess.Process] = set()


async def abort_all_procs(grace: float = 2.0) -> int:
    """杀掉所有登记在案的子进程树，返回处理条数（终止按钮的第二道保险）"""
    procs = list(_LIVE_PROCS) + list(_BG_PROCS)
    for p in procs:
        try:
            await _kill_tree(p, grace)
        except Exception:
            pass
        _LIVE_PROCS.discard(p)
        _BG_PROCS.discard(p)
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


async def _spawn_detached(command: str, env: dict, cwd: str | None = None) -> str:
    """把命令整个甩到后台跑，立刻返回；用于「要挂遮罩」的启动类命令

    用 setsid 脱离当前会话：后端退出时不会把用户刚打开的程序一起带走。
    启动后等 1.5s 看它是否立刻失败（命令写错、flatpak 名字不对之类），
    失败时把输出原样交回去，不让用户以为「打开了」。
    """
    # mkstemp 会返回一个已打开的 fd，这里只借它的文件路径，必须自己关掉，否则每跑一次漏一个
    _fd, _tmp = tempfile.mkstemp(prefix="deckmeido_launch_", suffix=".log")
    os.close(_fd)
    out_path = Path(_tmp)
    wrapped = (
        f"setsid bash -c {shlex.quote(command)} > {shlex.quote(str(out_path))} 2>&1 &"
        " echo $!"
    )
    proc = await asyncio.create_subprocess_shell(
        wrapped, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=cwd or str(Path.home()), env=env, executable="/bin/bash",
        start_new_session=True,
    )
    out, _ = await proc.communicate()
    pid = (out or b"").decode("utf-8", "replace").strip()
    # 与 _run_command 里的 launch_wait 保持一致：慢一点的启动器也要有机会
    # 把报错吐出来，不然会当成「已常驻」放过
    await asyncio.sleep(3.0)
    text = ""
    try:
        text = out_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    # 日志内容已经读进内存，临时文件不再需要：以前每个启动器都在 /tmp 漏一个
    try:
        out_path.unlink()
    except OSError:
        pass
    if pid and not Path(f"/proc/{pid}").exists():
        # 进程已经没了：把它的输出当成失败原因交回去
        return (f"启动后立即退出（pid {pid}）。\n{text}" if text
                else f"启动后立即退出（pid {pid}），没有任何输出。")
    msg = f"已在后台启动（pid {pid or '?'}）"
    return f"{msg}\n{text}" if text else msg


def _bg_drain(proc: asyncio.subprocess.Process) -> Path:
    """把仍在运行的进程转后台：开任务持续把 stdout 抄进日志文件并登记，返回日志路径

    进程的 stdout 还挂在我们创建的管道上，一旦没人读，输出写满管道缓冲区（64KB）
    它就会卡住，所以转后台时必须派人持续读。登记进 _BG_PROCS 是为了「终止」还能收掉它。
    """
    _fd, _tmp = tempfile.mkstemp(prefix="deckmeido_bg_", suffix=".log")
    log_path = Path(_tmp)
    fd = _fd

    async def _drain():
        try:
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                os.write(fd, chunk)
        except Exception:
            pass
        finally:
            try:
                os.close(fd)
            except Exception:
                pass

    task = asyncio.create_task(_drain())
    # 保住引用：asyncio 只持弱引用，不存一份会被 GC 连带取消
    _BG_DRAINERS.add(task)
    task.add_done_callback(_BG_DRAINERS.discard)
    _BG_PROCS.add(proc)
    task.add_done_callback(lambda _t, _p=proc: _BG_PROCS.discard(_p))
    return log_path


async def _read_probe(proc: asyncio.subprocess.Process, seconds: float) -> tuple[bytes, bool, bool]:
    """读 proc.stdout 最多 seconds 秒，返回 (输出, 是否已 EOF, 期间是否有输出)

    不取消读取（wait_for 超时只是停在这，stdout 之后还能继续读）。「是否有输出」
    用来区分常驻 GUI（启动后很快静默）和正在干活的长命令（持续吐输出）。
    """
    chunks: list[bytes] = []
    had_output = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return b"".join(chunks), False, had_output
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(8192), remaining)
        except asyncio.TimeoutError:
            return b"".join(chunks), False, had_output
        if not chunk:
            try:
                await proc.wait()
            except Exception:
                pass
            return b"".join(chunks), True, had_output
        chunks.append(chunk)
        had_output = True


async def _read_rest(proc: asyncio.subprocess.Process, budget: float) -> bytes:
    """继续读 proc.stdout 直到 EOF，最多再等 budget 秒；没读完抛 TimeoutError"""
    chunks: list[bytes] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(8192), remaining)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError
        if not chunk:
            try:
                await proc.wait()
            except Exception:
                pass
            return b"".join(chunks)
        chunks.append(chunk)


async def _run_command(command: str, reason: str = "", timeout: int = 60, stdin: str = "",
                       detach: bool = False) -> str:
    timeout = max(1, min(int(timeout or 60), 300))
    env = dict(os.environ)
    _fix_display_env(env)
    # 工作区边界对命令通道同样生效：manual / auto 下把 cwd 收进工作区，
    # 写入路径越界则直接拒绝。以前这里固定 cwd=家目录，等于「文件工具受管、
    # 一条 cat ~/.config/xxx 就能绕过」。
    cwd = str(Path.home())
    if path_bound:
        cwd = str(WORKSPACE)
        for raw in extract_writes(command):
            # 目标可能是变量或通配符，解析不了就跳过（交给审批与危险判定兜底）
            if any(ch in raw for ch in "$*?["):
                continue
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = Path(cwd) / target
            resolve_in_workspace(str(target))
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
    if detach:
        # 这次调用要挂遮罩让用户能随时点停，后端就不能堵在这里等程序退出
        # （QQ 这种会一直开着）。改成 setsid 放后台，输出重定向到文件，
        # 立刻把控制权还给 agent，遮罩只亮这一小会儿。
        return await _spawn_detached(command, env, cwd)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE if feed else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
        executable="/bin/bash",
        start_new_session=True,
    )
    _LIVE_PROCS.add(proc)
    launcher = group_file is not None
    # 启动类命令只等这么久：正常程序 1~2 秒内就出窗口或报错。
    # 给到 3 秒是为了让慢一点的启动器（flatpak、走 D-Bus 激活的）也有机会
    # 把报错吐出来，而不是被当成「已常驻」放过
    launch_wait = 3.0
    # 真·超时（命令卡死）要杀掉；启动器/常驻程序等满前只是「它在运行」，不能杀
    try:
        if launcher:
            # 白名单命中的常驻程序：等几秒看它是正常启动还是报错，能拿到输出就先交差
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), launch_wait)
                launcher_done = True
            except asyncio.TimeoutError:
                launcher_done = False
                out = b""
        elif feed is not None:
            # 交互式命令（带 stdin 输入）不能提前判常驻，走原来的等超时路径
            launcher_done = True
            out, _ = await asyncio.wait_for(proc.communicate(feed), timeout)
        else:
            # 通用探测：不靠程序名白名单猜「是不是常驻」。先读 launch_wait 秒：
            #   - 秒退          → 正常返回
            #   - 静默且还活着    → 常驻程序（GUI/游戏），立刻转后台，不再死等 timeout
            #   - 有输出且还活着  → 在干活（编译/下载等），继续读满 timeout
            launcher_done = True
            out, exited, had_output = await _read_probe(proc, launch_wait)
            if not exited and not had_output:
                log_path = _bg_drain(proc)
                return (f"已在后台启动（进程号 {proc.pid}）。"
                        f"启动后 {launch_wait:.0f}s 内程序没有输出且未退出，判定为常驻程序，"
                        f"已转入后台继续运行。如需确认是否正常，用 pgrep / 看窗口检查，不要重复启动。\n"
                        f"日志：{log_path}")
            if not exited:
                # 在干活：把剩余输出读到 EOF；总预算内没读完就交给下面的超时分支
                out2 = await _read_rest(proc, max(0.5, timeout - launch_wait))
                out = out + out2
    except asyncio.TimeoutError:
        # 超时不等于命令出错。还活着且撑满整个超时的，基本都是常驻程序。
        alive = proc.returncode is None
        if alive:
            log_path = _bg_drain(proc)
            return (f"命令运行超过 {timeout}s 仍未退出，判断是常驻程序，已转入后台继续运行"
                    f"（进程号 {proc.pid}）。\n"
                    f"如需确认它是否正常，用 pgrep / 看窗口 检查，不要重复启动。\n"
                    f"日志：{log_path}")
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

# 确认危险操作时认的字。只保留意思明确的词：「好」「可以」「ok」这类在
# 日常对话里太常见（「好的，顺便帮我查下天气」会被当成同意执行），去掉。
# 「执行」「y」「yes」同样太容易在别的话里出现（比如「请输入要执行的命令」），
# 危险操作只认明确表态，宁可让用户多说一个字
_CONFIRM_WORDS = {"确认", "确认执行", "确认一下", "同意执行"}

# 去掉首尾空白与标点后再比，避免「确认。」「确认！」认不出
_CONFIRM_STRIP = " \t\r\n。，,.!！?？~～"


def is_confirm(text: str) -> bool:
    return text.strip(_CONFIRM_STRIP).lower() in _CONFIRM_WORDS


def summarize_tool(name: str, args: dict) -> str:
    """工具调用的简短中文说明（作为界面提示，不含内部函数名和 JSON 括号）"""
    if name == "remember":
        t = str(args.get("text", ""))
        return t if len(t) <= 60 else t[:60] + "…"
    if name == "ask_user":
        q = str(args.get("question", ""))
        return q if len(q) <= 60 else q[:60] + "…"
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
        if act == "scroll":
            return f"滚动 dx={args.get('dx', 0)} dy={args.get('dy', 0)}"
        if act == "drag":
            return (f"从 ({args.get('x', 0)}, {args.get('y', 0)}) 拖到 "
                    f"({args.get('x2', 0)}, {args.get('y2', 0)})")
        if act == "swipe":
            return f"触摸板滑动 dx={args.get('dx', 0)} dy={args.get('dy', 0)}"
        if act == "type":
            t = str(args.get("text", ""))
            return f"输入「{t[:40]}」"
        return "查看光标与焦点"
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        path = str(args.get("path", ""))
        return path.split("/")[-1] or path
    if name == "gamepad":
        act = str(args.get("action", ""))
        if act == "stick":
            side = "右" if str(args.get("side", "left")) == "right" else "左"
            return f"推{side}摇杆 ({args.get('x', 0)}, {args.get('y', 0)})"
        if act == "trigger":
            return f"{str(args.get('button', '')).upper()} 扳机 {args.get('value', 1)}"
        if act == "release":
            return "摇杆回中、松开扳机"
        btn = str(args.get("button", "")).upper()
        if act == "hold":
            return f"按住 {btn} {args.get('ms', 500)}ms"
        return f"按 {btn}"
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


def _session_user() -> tuple[int, str] | None:
    """gamescope / PipeWire 所在会话的 (uid, 用户名)；找不到返回 None

    后端常以 root 跑（插件 flags 里有 root），此时 os.getuid() 是 0，
    而 gamescope 在 deck 用户的图形会话里，套接字在 /run/user/1000。
    直接用 getuid() 会连到 /run/user/0，抓帧必然失败。
    """
    import pwd

    if os.getuid() != 0:
        return os.getuid(), pwd.getpwuid(os.getuid()).pw_name
    # root：找一个真实登录的普通用户会话，优先 uid=1000（SteamOS 的 deck）
    candidates: list[tuple[int, str]] = []
    try:
        for d in Path("/run/user").iterdir():
            if not d.name.isdigit():
                continue
            uid = int(d.name)
            # 有 pulse 或 dbus 用户总线的才算活着的图形/音频会话
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


def _gamescope_pw_node_sync() -> str | None:
    """同步版：起 pw-dump 找 gamescope 的 PipeWire 视频节点 id；找不到返回 None

    这是「当前在不在游戏模式」的唯一权威判据（main.get_status、depcheck、
    stream 都复用同一条规则）。不能用 pgrep -x gamescope 代替——SteamOS 上
    进程名带参数时 comm 会被截断成 "gamescope"（15 字节上限）反而匹配不上，
    所以 pgrep 一度恒返回 1，桌面网页和插件就一直显示「桌面模式」。
    """
    sess = _session_user()
    env = dict(os.environ)
    if sess:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{sess[0]}"
    try:
        proc = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, env=env, timeout=10,
        )
        nodes = json.loads(proc.stdout or "[]")
        for o in nodes:
            props = (o.get("info") or {}).get("props") or {}
            if props.get("node.name") == "gamescope" and props.get("media.class") == "Video/Source":
                return str(o.get("id"))
    except Exception:
        return None
    return None


async def _gamescope_pw_node() -> str | None:
    """异步包装：抓帧路径用。逻辑与 _gamescope_pw_node_sync 完全相同，
    经 to_thread 复用同一份实现，避免和模式判断各写一套探针"""
    return await asyncio.to_thread(_gamescope_pw_node_sync)


def _raw_ok(raw: Path) -> bool:
    """抓帧产物是否有效：存在且大于 10KB（pngenc 失败时会留下空文件）"""
    try:
        return raw.exists() and raw.stat().st_size >= 10000
    except OSError:
        return False


# 「现在是不是游戏模式」的判据缓存：(是否游戏模式, 时间戳)。
# 判一次要起 pw-dump 并解析整份节点树，几十毫秒起步；状态是 3 秒一拉的，
# 不能每条都现算。切模式时最多晚 TTL 秒才反映过来，这是可接受的
_MODE_TTL = 5.0
_mode_cache: tuple[bool, float] = (False, 0.0)


def mode_is_gaming() -> bool:
    """当前是否游戏模式：PipeWire 里有 gamescope 的视频节点就是

    以前用 `pgrep -x gamescope` 判断，但 SteamOS 的 gamescope 是带一大串
    参数启动的，comm 被截断到 15 字节后反而不等于 "gamescope"，pgrep 恒返回 1，
    于是就出现了「人在游戏模式里、界面一直显示桌面模式、灯也不对」。
    现在统一用 _gamescope_pw_node 这一个判据：它正是抓帧/推流用的那条路径，
    判断和实际能力不会再出现两套结论。
    """
    global _mode_cache
    ok, at = _mode_cache
    if time.monotonic() - at < _MODE_TTL:
        return ok
    ok = _mode_probe_sync()
    _mode_cache = (ok, time.monotonic())
    return ok


def _mode_probe_sync() -> bool:
    """同步跑一次探针。纯同步实现，事件循环内外都能安全调用

    以前这里用 asyncio.run(_gamescope_pw_node())，一旦调用点正好处于运行中的
    事件循环（比如广播状态时 get_status() 直接在 async 里同步调用），缓存过期
    那一刻就会抛「asyncio.run() cannot be called from a running event loop」。
    改成同步探针后这个隐患没有了。
    """
    return _gamescope_pw_node_sync() is not None


async def _gst_grab(node: str, raw: Path, uid: int, user: str,
                    timeout: float) -> tuple[bool, str]:
    """跑一次 gst-launch 把一帧写进 raw。返回 (成功, 错误文字)"""
    env = dict(os.environ)
    # 必须指向 gamescope 所属会话，不能是后端自己的 uid（root 时是 /run/user/0）
    env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    # gst-launch 把每个 argv 当成一个独立 token，元素名和属性必须分开传，
    # 写成 "pipewiresrc path=78" 会被判成语法错误
    cmd = [
        "gst-launch-1.0", "-q", "pipewiresrc", f"path={node}", "num-buffers=2",
        "do-timestamp=true", "!", "videoconvert", "!", "pngenc", "!",
        "filesink", f"location={raw}",
    ]
    # root 跑时降权到会话用户：PipeWire 的 socket 权限按用户隔离，root 直连会被拒
    if os.getuid() == 0 and uid != 0:
        cmd = ["runuser", "-u", user, "--", *cmd]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            err_text = (err or b"").decode(errors="replace").strip()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, ""  # 空字符串表示「就是没等到帧」，跟真正的报错区分开
        if not _raw_ok(raw):
            # gst 的具体报错比「失败了」有用得多，取最后一行
            line = err_text.splitlines()[-1] if err_text else ""
            return False, line or "（gst 没有输出错误信息）"
        return True, ""
    except Exception as exc:
        return False, f"抓帧进程启动失败：{exc}"


# 抓帧时「戳一下重绘」的摇杆幅度：3% 量程，拨 40ms 回中
_POKE_AMT = 0.03


async def _poke_redraw() -> None:
    """让画面动一下，逼 gamescope 吐帧

    gamescope 只在画面内容变化时才往 PipeWire 投帧。游戏静止时（模拟器停在
    列表页、玩家没在操作）pipewiresrc 连上去会一直空等到超时——实测挂 60 秒
    一帧都没有，journal 里只有 streaming → paused。

    这里轻推一下右摇杆再回中（幅度 3%，拨 40ms）：模拟器菜单里相当于没动，
    3D 游戏里就是视角极轻微晃一下。用后端自己那套 uinput 虚拟手柄，不依赖
    xdotool 和 DISPLAY。
    """
    try:
        await _PAD.stick(_POKE_AMT, _POKE_AMT, "right")
        await asyncio.sleep(0.04)
        await _PAD.stick(0.0, 0.0, "right")
    except Exception:
        pass


async def _screenshot_gamescope(path: Path) -> str:
    """游戏模式：从 gamescope 的 PipeWire 视频节点抓一帧，成功返回空串，失败返回原因

    失败原因必须带出来：以前这里只返回 True/False，模型只能收到一句笼统的
    「抓帧失败」，界面上也看不出「这张图没抓到」，用户会以为模型真的看了画面
    然后按画布胡答。
    """
    node = await _gamescope_pw_node()
    if not node:
        return "没找到 gamescope 的 PipeWire 视频节点（pw-dump 里没有 node.name=gamescope）"
    if not shutil.which("gst-launch-1.0"):
        return "没装 gst-launch-1.0（gstreamer1.0-tools），抓不了帧"
    sess = _session_user()
    if not sess:
        return "找不到 gamescope 所在的用户会话（/run/user/* 下没有活的图形会话）"
    uid, user = sess
    raw = path.with_suffix(".raw.png")
    # 先试抓 1.5 秒：画面在动就立刻拿到，静止才走下面的「戳一下重绘」
    ok, err_text = await _gst_grab(node, raw, uid, user, timeout=1.5)
    if not ok and not err_text:
        await _poke_redraw()
        ok, err_text = await _gst_grab(node, raw, uid, user, timeout=12)
        if not ok and not err_text:
            return "抓帧超时（画面一直没有变化，推了一下画面也没等到新帧）"
    if not ok:
        return "抓帧没拿到有效图像" + (f"：{err_text}" if err_text else "")
    # pngenc 连续多帧会覆盖同一文件，取到的就是最后一帧，直接搬过来
    raw.replace(path)
    return ""


# ---------- 虚拟手柄（uinput） ----------
#
# 游戏模式下 XTest / Wayland 虚拟键盘都进不了 gamescope，能进游戏的只有
# /dev/uinput 虚拟设备（Steam Input、Sunshine 走的都是这条）。这里直接建一个
# 带 EV_KEY + EV_ABS 的手柄设备：键盘设备扛不了摇杆（ABS 是绝对值轴），
# 手柄设备也发不出键盘字符，两类设备分工不同。
#
# 与 plugin/main.py 里的 _Gamepad 是同一套实现，但那份只服务 QAM 面板，
# 后端跑在服务进程里拿不到它，所以这边必须自带一份。

_UI_DEV_CREATE = 0x5501
_UI_DEV_DESTROY = 0x5502
_UI_SET_EVBIT = 0x40045564
_UI_SET_KEYBIT = 0x40045565
_UI_SET_ABSBIT = 0x40045567
_UI_DEV_SETUP = 0x405C5503
_UI_ABS_SETUP = 0x401C5504

_EV_SYN, _EV_KEY, _EV_ABS = 0x00, 0x01, 0x03
_ABS_X, _ABS_Y, _ABS_Z, _ABS_RX, _ABS_RY, _ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
_STICK_MAX = 32767
_TRIG_MAX = 32767

# 面板/工具里用的名字 → 键位。ABXY 按 Xbox 布局。
_PAD_KEYS = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134,
    "lb": 0x136, "rb": 0x137,
    "up": 0x220, "down": 0x221, "left": 0x222, "right": 0x223,
    "start": 0x13B, "select": 0x13A,
    # BTN_MODE：Xbox 手柄的 Guide 键。SteamOS 里物理 Steam 键由系统层拦截，
    # 虚拟手柄发的这个键只会被游戏当成 Xbox/Guide 键，不会真的呼出 Steam 菜单
    "guide": 0x13C,
    # 摇杆按下（L3 / R3）
    "l3": 0x13D, "r3": 0x13E,
    # 分享键：Xbox Series X|S 才有，内核里报成 KEY_RECORD 而不是 BTN_*
    "share": 0x0A7,
}

# 扳机名字 → 轴。手柄惯例走 0..32767，不是按键
_PAD_TRIGGERS = {"lt": _ABS_Z, "rt": _ABS_RZ}

# 当前摇杆/扳机状态，用来判断 release 时要不要真的发一帧
_PAD_STICK = [0, 0]
_PAD_STICK_R = [0, 0]
_PAD_TRIG = {"lt": 0, "rt": 0}


class _VirtualPad:
    """一个懒创建、闲置自动销毁的虚拟手柄。

    一直挂着设备会让 Steam 认为接了手柄（可能强制切成手柄导航），
    所以闲置 _IDLE 秒后销毁。
    """

    _IDLE = 90.0

    def __init__(self):
        self._fd: int | None = None
        self._lock = asyncio.Lock()
        self._idle: asyncio.Task | None = None
        self._gen = 0

    def _emit(self, typ: int, code: int, value: int):
        # input_event 的 timeval 两个字段都是 64 位 long，所以用 qq 不用 ll
        import struct

        os.write(self._fd, struct.pack("qqHHi", 0, 0, typ, code, value))

    def _syn(self):
        self._emit(_EV_SYN, 0, 0)

    def _create(self):
        import fcntl
        import struct

        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_SYN)
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_ABS)
            for code in _PAD_KEYS.values():
                fcntl.ioctl(fd, _UI_SET_KEYBIT, code)
            for code, lo, hi in (
                (_ABS_X, -_STICK_MAX, _STICK_MAX),
                (_ABS_Y, -_STICK_MAX, _STICK_MAX),
                # 右摇杆：不少游戏用右摇杆转视角
                (_ABS_RX, -_STICK_MAX, _STICK_MAX),
                (_ABS_RY, -_STICK_MAX, _STICK_MAX),
                (_ABS_Z, 0, _TRIG_MAX),
                (_ABS_RZ, 0, _TRIG_MAX),
            ):
                fcntl.ioctl(fd, _UI_SET_ABSBIT, code)
                # uinput_abs_setup：code(2)+pad(2)+absinfo{value,min,max,fuzz,flat,resolution}
                fcntl.ioctl(
                    fd, _UI_ABS_SETUP,
                    struct.pack("HHiiiiii", code, 0, 0, lo, hi, 0, 0, 0),
                )
            # uinput_setup：id{bus,vendor,product,version} + name(80) + ff_effects_max
            fcntl.ioctl(
                fd, _UI_DEV_SETUP,
                struct.pack("HHHH80sI", 0x03, 0x045E, 0x02FD, 0x0001, b"DeckMeido Gamepad", 0),
            )
            fcntl.ioctl(fd, _UI_DEV_CREATE)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

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
        _PAD_STICK[0] = _PAD_STICK[1] = 0
        _PAD_STICK_R[0] = _PAD_STICK_R[1] = 0
        _PAD_TRIG["lt"] = _PAD_TRIG["rt"] = 0

    def _touch(self):
        if self._idle is not None:
            self._idle.cancel()
        # 代次自增：被取消的旧任务如果已经醒来并卡在等锁上，取消是打不断的
        # （CancelledError 只在 sleep 阶段生效）。它拿到锁后会比对代次，
        # 发现自己过期就直接退出，不会把刚建好的设备关掉。
        self._gen += 1
        self._idle = asyncio.create_task(self._close_later(self._gen))

    async def _close_later(self, gen: int):
        try:
            await asyncio.sleep(self._IDLE)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if gen != self._gen:
                return
            self._destroy()

    async def _ensure(self):
        if self._fd is None:
            self._create()
            # 设备刚创建时内核还没生成节点，游戏侧要一点时间才枚举到
            await asyncio.sleep(0.15)

    async def key(self, name: str, hold_ms: int = 80) -> str:
        code = _PAD_KEYS.get(name)
        if code is None:
            return f"不认识的手柄键：{name}（可用：{'、'.join(_PAD_KEYS)}）"
        async with self._lock:
            try:
                await self._ensure()
                self._emit(_EV_KEY, code, 1)
                self._syn()
                await asyncio.sleep(max(30, min(hold_ms, 5000)) / 1000)
                self._emit(_EV_KEY, code, 0)
                self._syn()
            except FileNotFoundError:
                return "手柄注入失败：没有 /dev/uinput，uinput 内核模块未加载"
            except PermissionError:
                return "手柄注入失败：没有写 /dev/uinput 的权限（需要 root）"
            self._touch()
        return f"已按下手柄键 {name}（{max(30, min(hold_ms, 5000))}ms）"

    async def stick(self, x: float, y: float, side: str = "left") -> str:
        right = str(side or "left").strip().lower() == "right"
        axes = (_ABS_RX, _ABS_RY) if right else (_ABS_X, _ABS_Y)
        vx = int(max(-1.0, min(1.0, x)) * _STICK_MAX)
        vy = int(max(-1.0, min(1.0, y)) * _STICK_MAX)
        async with self._lock:
            try:
                await self._ensure()
                self._emit(_EV_ABS, axes[0], vx)
                self._emit(_EV_ABS, axes[1], vy)
                self._syn()
            except FileNotFoundError:
                return "摇杆注入失败：没有 /dev/uinput，uinput 内核模块未加载"
            except PermissionError:
                return "摇杆注入失败：没有写 /dev/uinput 的权限（需要 root）"
            state = _PAD_STICK_R if right else _PAD_STICK
            state[0], state[1] = vx, vy
            self._touch()
        return f"{'右' if right else '左'}摇杆已推到 ({x:.2f}, {y:.2f})"

    async def trigger(self, which: str, value: float) -> str:
        name = str(which or "").strip().lower()
        code = _PAD_TRIGGERS.get(name)
        if code is None:
            return f"不认识的扳机：{which}（可用：lt、rt）"
        level = int(max(0.0, min(1.0, value)) * _TRIG_MAX)
        async with self._lock:
            try:
                await self._ensure()
                self._emit(_EV_ABS, code, level)
                self._syn()
            except FileNotFoundError:
                return "扳机注入失败：没有 /dev/uinput，uinput 内核模块未加载"
            except PermissionError:
                return "扳机注入失败：没有写 /dev/uinput 的权限（需要 root）"
            _PAD_TRIG[name] = level
            self._touch()
        return f"{name.upper()} 扳机力度 {value:.2f}"


_PAD = _VirtualPad()


async def _gamepad(action: str, button: str = "", ms: int = 0,
                   x: float = 0.0, y: float = 0.0, side: str = "left",
                   value: float = 1.0) -> str:
    """模拟手柄。press/hold 按键，stick 推摇杆，trigger 压扳机，release 摇杆/扳机回中"""
    act = str(action or "").strip().lower()
    if act in ("press", "hold"):
        name = str(button or "").strip().lower()
        if not name:
            return "press/hold 需要填 button"
        # lt/rt 是轴不是键，按一下等价于踩到底再松开
        if name in _PAD_TRIGGERS:
            default = 80 if act == "press" else 500
            hold = max(30, min(int(ms or default), 5000))
            await _PAD.trigger(name, 1.0)
            await asyncio.sleep(hold / 1000)
            return await _PAD.trigger(name, 0.0) + f"（按住 {hold}ms）"
        default = 80 if act == "press" else 500
        return await _PAD.key(name, int(ms or default))
    if act == "stick":
        try:
            return await _PAD.stick(float(x), float(y), side)
        except (TypeError, ValueError):
            return "stick 的 x/y 要填 -1..1 的数字"
    if act == "trigger":
        try:
            return await _PAD.trigger(button, float(value))
        except (TypeError, ValueError):
            return "trigger 的 value 要填 0..1 的数字"
    if act == "release":
        if str(button or "").strip().lower() in _PAD_TRIGGERS:
            return await _PAD.trigger(button, 0.0)
        out = [await _PAD.stick(0.0, 0.0, side)]
        # 扳机也一起松开，避免 release 之后还踩着油门
        for name in _PAD_TRIGGERS:
            if _PAD_TRIG[name]:
                out.append(await _PAD.trigger(name, 0.0))
        return "；".join(out)
    return f"不认识的 action：{action}（可用：press / hold / stick / trigger / release）"


async def _screenshot() -> str:
    """截取当前画面（游戏模式走 gamescope 的 PipeWire 节点，桌面模式用 spectacle/mss）"""
    shots_dir = Path(__file__).resolve().parent / "static" / "shots"
    shots_dir.mkdir(exist_ok=True)
    # 历史里只留 [SHOT:文件名] 标记，发给模型时才按文件名回读（_shot_ref_content），
    # 所以这里删掉的文件不只是「看不到了」，模型也读不到那张图了。
    # 以前只留 20 张，模型连截几十张就会把较早的那张删掉，历史里那条提问
    # 静默变成「界面破图 + 模型少看一张」。门槛提到和随附画面一致。
    old = sorted(shots_dir.glob("shot_*.png"), key=lambda p: p.stat().st_mtime)
    for p in old[:-900]:
        p.unlink(missing_ok=True)
    # vision_*.jpg 每截一张生成一份，以前从没清过，跑久了 shots/ 会被它们塞满
    old_v = sorted(shots_dir.glob("vision_*.jpg"), key=lambda p: p.stat().st_mtime)
    for p in old_v[:-900]:
        p.unlink(missing_ok=True)
    # 微秒级：同一秒内连截两张会互相覆盖
    name = f"shot_{int(time.time() * 1_000_000)}.png"
    path = shots_dir / name

    # 每次截图先把上一次的状态清干净，否则「上一张成功、这一张失败」时
    # 还留着旧图，模型会拿着上一帧回答。
    # 状态放在 SHOT_STATE 里而不是模块全局：两轮并发时后用模块全局会出现
    # B 截图清掉 A 的图、A 读不到画面却没有任何提示的情况
    holder = SHOT_STATE
    holder.clear()

    # 游戏模式：桌面会话已经结束，只能从 gamescope 抓
    if await _gamescope_pw_node():
        err = await _screenshot_gamescope(path)
        if not err:
            return await _finish_shot(path, name, shots_dir, time)
        holder["error"] = err
        return (f"截图失败：{err}。现在没有当前画面可用，"
                "请直接告诉用户这次没截到图、以及原因，不要凭空描述屏幕内容。")

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
            # spectacle 不可用或截到黑屏（Xwayland rootless）时退回 mss。
            # 没装 mss 时 import 会抛 ModuleNotFoundError，以前直接把工具调用
            # 炸掉，下面那句「都不可用」的提示根本走不到
            try:
                import mss
                with mss.mss() as sct:
                    sct.shot(output=str(path))
            except Exception:
                pass
    finally:
        await _set_overlay_lift(False)
    if not path.exists() or path.stat().st_size < 10000:
        holder["error"] = "spectacle 与 mss 都不可用（或截到的是黑色画面）"
        # 黑屏也会留下几十字节的残片，不删掉下次会被当成「已有截图」
        path.unlink(missing_ok=True)
        return (f"截图失败：{holder['error']}。现在没有当前画面可用，"
                "请直接告诉用户这次没截到图，不要凭空描述屏幕内容。")
    return await _finish_shot(path, name, shots_dir, time)


async def _finish_shot(path: Path, name: str, shots_dir: Path, time_mod) -> str:
    """压缩一份给多模态模型看（1280 宽 JPEG），并返回给模型的结果文本"""
    holder = SHOT_STATE
    holder.pop("vision", None)
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((1280, 1280))
        vpath = shots_dir / f"vision_{int(time_mod.time() * 1_000_000)}.jpg"
        img.convert("RGB").save(vpath, "JPEG", quality=70)
        holder["vision"] = vpath
    except Exception as e:
        # 以前这里是静默 pass：PIL 没装或图坏了，模型就收不到画面，
        # 但工具结果照样说「当前画面已附上」，排查时无从下手
        holder["error"] = f"画面压缩失败（{e!r}）"
        return (f"截图完成：/shots/{name}，但压缩给模型看的那份失败了"
                f"（{holder['error']}）。这次你收不到画面，"
                "请直接告诉用户没看到图，不要凭空描述屏幕内容。")
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


async def _klipper_paste(text: str) -> str | None:
    """用 KDE 原生剪贴板 + Ctrl+V 输入文本。成功返回 None，失败返回原因

    为什么不用 ydotool type 打中文：ydotool 只能按 keycode 逐个敲物理按键，
    中文这类多字节字符没有对应的键位，会被整个丢掉（英文数字才正常）。
    wtype 依赖虚拟键盘协议，KWin 不支持，直接报 Compositor does not support
    the virtual keyboard protocol。GTK 那条路会自己占住剪贴板，Wayland 下
    回读是 None。

    能走通的只有 klipper 的 D-Bus 接口：它把文字交给 KDE 自己的剪贴板管理器，
    再用 Ctrl+V 让目标窗口粘贴，中英混排、长文本都是原样进去的。
    """
    if not text:
        return "文本为空"
    env = dict(os.environ)
    _fix_display_env(env)
    # qdbus 和 qdbus6 在不同 Plasma 版本上装的不一样，哪个能跑用哪个
    last_err = ""
    ok = False
    for tool in ("qdbus6", "qdbus"):
        if not shutil.which(tool):
            continue
        proc = await asyncio.create_subprocess_exec(
            tool, "org.kde.klipper", "/klipper",
            "org.kde.klipper.klipper.setClipboardContents", text,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        out, err = await proc.communicate()
        last_err = (out + err).decode(errors="replace").strip()
        if proc.returncode == 0:
            ok = True
            break
    if not ok:
        return f"调用 klipper 失败：{last_err or '找不到 qdbus6/qdbus'}"
    # 让 klipper 拿到内容再发粘贴键，否则目标窗口粘出来还是上一次的内容
    await asyncio.sleep(0.15)
    try:
        await _ydotool("key", "29:1", "47:1", "47:0", "29:0")
    except Exception as e:
        return f"已写入剪贴板，但 Ctrl+V 失败：{e}"
    return None


async def _type_text(text: str) -> str:
    """在当前焦点窗口输入文本，中英文都支持

    优先走 klipper 剪贴板粘贴（唯一能正确处理中文的方式），
    它不可用时再退回 ydotool type（只适合英文/数字/符号）。
    """
    err = await _klipper_paste(text)
    if err is None:
        return f"已输入 {len(text)} 个字符（经 KDE 剪贴板粘贴）"
    # 退路：纯 ASCII 时 ydotool 逐个敲键是可靠的
    if text.isascii():
        await _ydotool("type", "--", text)
        return f"已输入 {len(text)} 个字符（ydotool 逐键输入）"
    return (f"输入失败：{err}。含中文的内容只能靠 KDE 剪贴板粘贴，"
            f"请确认 KDE 剪贴板服务 klipper 正在运行")


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


async def _app_running(keyword: str) -> bool:
    """按名字粗略判断进程是否还活着（用于区分「没启动」和「藏在托盘里」）"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-fi", keyword,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return proc.returncode == 0 and bool(out.strip())
    except Exception:
        return False


# 滚轮一格约等于 120 像素的视线位移；模型给的是截图上的像素，
# 这里统一换算成 ydotool 的格数，避免各调用点各写一套系数
WHEEL_PX_PER_STEP = 120
# 触摸板滑动的格间间隔：太密目标程序会并成一格，太疏又不像一次手势
_SWIPE_INTERVAL = 0.02
# 拖拽最多插这么多步：全程每步都要读一次光标，几十步就够平滑了，
# 上千像素的长拖按 80 步切，单步位移大一点也仍然像「拖」而不是「瞬移」
_DRAG_MAX_STEPS = 80


def _wheel_steps(dx: int, dy: int) -> tuple[int, int]:
    """像素位移 → 滚轮格数。不足一格的按一格算，非零方向不能丢"""
    def conv(v: int) -> int:
        v = int(v or 0)
        if not v:
            return 0
        n = int(round(abs(v) / WHEEL_PX_PER_STEP))
        return (1 if v > 0 else -1) * max(1, n)
    return conv(dx), conv(dy)


def _drag_steps(x: int, y: int, x2: int, y2: int) -> list[tuple[int, int]]:
    """起点到终点的插值路径（不含起点本身）"""
    dist = max(abs(x2 - x), abs(y2 - y), 1)
    n = max(2, min(_DRAG_MAX_STEPS, dist // 8 or 2))
    return [(x + (x2 - x) * i // n, y + (y2 - y) * i // n) for i in range(1, n + 1)]


async def _desktop(action: str, target: str = "", x: int = 0, y: int = 0, text: str = "",
                   x2: int = 0, y2: int = 0, dx: int = 0, dy: int = 0) -> str:
    """桌面窗口操作：切焦点 / 点击 / 移动 / 滚动 / 拖拽 / 触摸板滑动 / 打字

    按下鼠标键的动作（点击、滚动、拖拽、滑动）执行前都会先让遮罩整块让开，
    否则那一下会打在遮罩上被当成「用户终止」。
    """
    if action == "focus":
        if not target:
            return "focus 需要提供 target（窗口标题关键词）"
        code, out = await _kdotool("search", "--name", target)
        if code != 0 or not out:
            # 进程活着却搜不到窗口，绝大多数是程序被最小化到了系统托盘：
            # 托盘里的项不是普通窗口，kdotool search / windowactivate 都碰不到，
            # 面板的 D-Bus 也没有通用的 Activate 方法。这种情况只能让程序
            # 重新把主窗口建出来 —— 杀掉再启动是最稳的。
            alive = await _app_running(target)
            hint = (f"\n进程「{target}」还在运行，但没有任何窗口。"
                    f"它很可能缩在系统托盘里，托盘项不能被 kdotool 激活。"
                    f"可靠做法：先结束进程再重新启动，主窗口就会回来"
                    f"（例如 pkill -f {target} 后用原来的启动命令再打开）。"
                    if alive else "")
            return f"没找到标题匹配「{target}」的窗口{hint}"
        wid = out.splitlines()[0].strip()
        await _kdotool("windowactivate", wid)
        await _kdotool("windowraise", wid)
        _, name = await _kdotool("getwindowname", wid)
        return f"已切到前台：{name}"

    if action in ("move", "click"):
        # 遮罩盖在桌面最上层，而且它是 XWayland 窗口：Wayland 合成器做命中测试时
        # 看不到 X11 的 input shape，所以只能让遮罩整块藏起来（后端会等遮罩确认
        # 藏好再放行）。不这么做，点击会被当成「用户终止」
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

    if action == "scroll":
        # ydotool 的滚动是按格数（一把 0x40 横向、0x41 纵向，值带符号），
        # 而模型看截图给的是像素，换算见 _wheel_steps
        nx, ny = _wheel_steps(dx, dy)
        if not nx and not ny:
            return "scroll 需要提供非零的 dx 或 dy"
        await _set_overlay_passthrough(True)
        try:
            # x/y 省掉时沿用当前光标位置，省得模型每次都要先 where 一下
            if x or y:
                await _move_to(x, y)
                await asyncio.sleep(0.1)
            if ny:
                await _ydotool("click", f"0x41:{ny}")
            if nx:
                await _ydotool("click", f"0x40:{nx}")
            cx, cy = await _cursor()
            return (f"已在 ({cx}, {cy}) 滚动：横向 {nx} 格、纵向 {ny} 格"
                    f"（原始位移 dx={dx}, dy={dy}）")
        finally:
            await _set_overlay_passthrough(False)

    if action == "drag":
        if (x, y) == (x2, y2):
            return "drag 的起点和终点相同，拖不出效果；请提供不同的 x2/y2"
        steps = _drag_steps(x, y, x2, y2)
        await _set_overlay_passthrough(True)
        try:
            await _move_to(x, y)
            await asyncio.sleep(0.1)
            # 按下 → 分步移动 → 松开。中间不插值的话目标程序只看到一次跳跃，
            # 滑块/列表拖选这类按位移判断的逻辑会当成「没拖」
            await _ydotool("click", "0x40:1")
            for px, py in steps:
                await _move_to(px, py)
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            await _ydotool("click", "0x40:0")
            cx, cy = await _cursor()
            return f"已从 ({x}, {y}) 拖到 ({cx}, {cy})，共 {len(steps)} 步"
        finally:
            await _set_overlay_passthrough(False)

    if action == "swipe":
        if not dx and not dy:
            return "swipe 需要提供非零的 dx 或 dy（单位是屏幕像素）"
        await _set_overlay_passthrough(True)
        try:
            # 触摸板滑动本质是「连续的小幅滚轮」：按步长切成若干格，
            # 每格之间留一点间隔，目标程序才会把它当成一次连续手势
            total_x, total_y = _wheel_steps(dx, dy)
            px = x
            py = y
            if px or py:
                await _move_to(px, py)
            per_x = -1 if total_x < 0 else 1
            per_y = -1 if total_y < 0 else 1
            left_x, left_y = abs(total_x), abs(total_y)
            sent = 0
            while left_x or left_y:
                one_x = per_x if left_x else 0
                one_y = per_y if left_y else 0
                if one_x:
                    await _ydotool("click", f"0x40:{one_x}")
                    left_x -= 1
                if one_y:
                    await _ydotool("click", f"0x41:{one_y}")
                    left_y -= 1
                sent += 1
                await asyncio.sleep(_SWIPE_INTERVAL)
            return (f"已模拟触摸板滑动：dx={dx}, dy={dy} 像素"
                    f"（换算成 {abs(total_x)} 格横向、{abs(total_y)} 格纵向，分 {sent} 步）")
        finally:
            await _set_overlay_passthrough(False)

    if action == "type":
        if not text:
            return "type 需要提供 text"
        # 输入前若焦点不在目标窗口，调用方应先 focus
        return await _type_text(text)

    if action == "where":
        cx, cy = await _cursor()
        _, focus = await _kdotool("getactivewindow", "getwindowname")
        return f"光标：({cx}, {cy})\n焦点窗口：{focus}"

    return f"不支持的动作：{action}"


# 本轮截图的状态。run_agent 每轮进来时换成一个新的 dict（见 new_shot_state），
# 于是两轮并发时各读各的，不会再互相清空。
#   vision: 模型可读的 JPEG 路径；error: 这次截图失败的原因
SHOT_STATE: dict = {}


def new_shot_state() -> dict:
    """给一轮对话分配独占的截图状态容器，返回并设为当前"""
    global SHOT_STATE
    SHOT_STATE = {}
    return SHOT_STATE

# 最近一次文件写入/编辑的增删行数（供 UI 展示 diff 统计）
LAST_DIFF: dict | None = None


WORKSPACE = Path.home() / "deckmeido_workspace"


def set_workspace(path: str):
    """切换工作区目录（设置面板可改），目录不存在则创建"""
    global WORKSPACE
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    WORKSPACE = p.resolve()


# 文件工具是否限制在工作区内。main 在权限模式变化时同步开启/关闭
path_bound = True


# 不受工作区约束的路径白名单（绝对路径，已 resolve）。
# 想放开别处就改这里，不要在调用点临时判断
FREE_PATHS: list[Path] = []


class OutsideWorkspace(Exception):
    """路径落在工作区之外，且当前权限模式不允许越界"""


def resolve_in_workspace(path: str) -> Path:
    """把用户/模型给的路径解析成绝对路径，并检查是否允许访问。

    只做 expanduser + resolve（会跟着符号链接走），所以「跳出工作区再跳回来」
    和「工作区里放个指向 / 的软链」这两种绕法都会落在检查之后。
    full 模式下不设限，与 run_command 的边界保持一致；manual / auto 只能
    碰工作区，避免一句「读一下 config.toml」把 API Key 连带会话内容送给上游。
    """
    p = Path(path).expanduser().resolve()
    if not path_bound:
        return p
    for root in [WORKSPACE, *FREE_PATHS]:
        if p == root or root in p.parents:
            return p
    raise OutsideWorkspace(str(p))


async def _list_dir(path: str = "") -> str:
    d = resolve_in_workspace(path) if path else WORKSPACE
    if not path:
        d.mkdir(parents=True, exist_ok=True)
    if not d.is_dir():
        return f"不是目录：{d}"
    entries = []
    for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
        tag = "[目录]" if p.is_dir() else "[文件]"
        size = f" ({p.stat().st_size}B)" if p.is_file() else ""
        entries.append(f"{tag} {p.name}{size}")
    return f"{d} 共 {len(entries)} 项：\n" + ("\n".join(entries[:200]) or "（空）")


async def _read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    p = resolve_in_workspace(path)
    if not p.is_file():
        return f"文件不存在：{p}"
    # 大文件整份读进内存会直接把后端撑爆（几百 MB 的日志很常见）
    if p.stat().st_size > 8 * 1024 * 1024:
        return f"文件太大（{p.stat().st_size / 1048576:.1f}MB），请改用 run_command 里 head/tail/grep 查看片段：{p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    offset = max(1, offset)
    chunk = lines[offset - 1: offset - 1 + min(limit, 2000)]
    body = "\n".join(f"{offset + i}→{line}" for i, line in enumerate(chunk))
    note = f"（共 {len(lines)} 行，显示 {offset}-{offset - 1 + len(chunk)}）"
    return f"{p} {note}\n{body}"


def _atomic_write(p: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace：写一半被杀不会留下半截文件"""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


async def _write_file(path: str, content: str) -> str:
    global LAST_DIFF
    LAST_DIFF = None
    p = resolve_in_workspace(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    old_lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines()) if p.is_file() else 0
    _atomic_write(p, content)
    LAST_DIFF = {"add": len(content.splitlines()), "del": old_lines}
    return f"已写入 {p}（{len(content)} 字符）"


async def _edit_file(path: str, old: str, new: str) -> str:
    global LAST_DIFF
    LAST_DIFF = None
    p = resolve_in_workspace(path)
    if not p.is_file():
        return f"文件不存在：{p}"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "替换失败：old 文本在文件中不存在（先用 read_file 确认原文）"
    if count > 1:
        return f"替换失败：old 文本出现了 {count} 次，请扩大范围使其唯一"
    _atomic_write(p, text.replace(old, new, 1))
    LAST_DIFF = {"add": len(new.splitlines()), "del": len(old.splitlines())}
    return f"已修改 {p}（替换 1 处）"


async def _remember(text: str, kind: str = "note") -> str:
    """写入长期记忆。重复内容不会存第二遍，回给模型的说明要能区分这两种情况"""
    from . import memory
    text = str(text or "").strip()
    if not text:
        return "没有提供要记住的内容。"
    existed = any(memory.norm(it["text"]) == memory.norm(text)
                  for it in memory.load()["items"])
    item = memory.add(kind, text)
    if not item:
        return "没有提供要记住的内容。"
    label = memory.KINDS[item["kind"]]
    if existed:
        return f"这条已经在长期记忆里了（{label}）：{item['text']}"
    return f"已记入长期记忆（{label}）：{item['text']}"


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
    "gamepad": _gamepad,
    "screenshot": _screenshot,
    "list_dir": _list_dir,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "self_update": _self_update,
    "remember": _remember,
}


# 不需要「保持常亮」的工具：查天气、联网搜索、读写文件这类不碰屏幕也不动
# 设备状态的动作，几毫秒就结束，挂抑制反而是白折腾（每次都 fork 两个外部命令）
_NO_KEEP_AWAKE = {
    "get_weather", "web_search", "find_game", "list_installed_games",
    "list_emulator_roms", "check_system_updates", "screenshot", "remember",
}


async def execute(name: str, args: dict) -> str:
    fn = EXECUTORS.get(name)
    if not fn:
        return f"未知工具：{name}"
    args = dict(args)
    # 执行期间别让屏幕黑掉/机器睡着：run_command 可能跑几分钟，
    # 用户回来看到的应该是进度，而不是一个锁屏界面。
    # 桌面模式判断和非空原因计数都在 power 里，游戏模式/纯查询工具会自动跳过。
    keep = name not in _NO_KEEP_AWAKE
    if keep:
        power.keeper.hold("command", f"正在{cn_name(name)}")
    try:
        # 启动类命令（打开应用）会挂遮罩让用户能随时点停，就不能堵在这里等它退出：
        # 打开 QQ 之后后台会一直卡在 communicate()，遮罩一直亮着空转
        if name == "run_command" and _is_launcher(str(args.get("command", ""))) \
                and not args.get("stdin"):
            args["detach"] = True
        return await fn(**args)
    except OutsideWorkspace as e:
        # 单独给一句能看懂的：越界是权限策略而不是文件坏了，
        # 模型该停下来问用户，而不是换个路径继续试
        return (f"拒绝访问：{e} 在工作区 {WORKSPACE} 之外。"
                f"当前权限模式只允许操作工作区内的文件；"
                f"确实需要访问这里，请让用户去设置里切到「完全访问」")
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"
    finally:
        if keep:
            power.keeper.release("command")
