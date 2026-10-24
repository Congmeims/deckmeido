"""DeckMeido 工具层：供 LLM function calling 调用的本机能力"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import shutil
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

# 会实际控制设备的工具：只有这些才弹出操作遮罩。
# 联网搜索、查天气、读写文件等都只是后台任务，遮罩会白挡一层，还会被误当成「正在操作」；
# 截屏是只读采集，且遮罩本身会被拍进截图里，所以也不弹
DEVICE_TOOLS = {"desktop", "run_command"}

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


# 命令里出现这些说明 agent 要真的点屏幕：ydotool/xdotool 的 click、mousedown、mouseup
_MOUSE_CLICK_RE = re.compile(
    r"\b(?:ydotool|xdotool)\b[^\n;&|]*\b(?:click|mousedown|mouseup)\b")

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


async def _run_command(command: str, reason: str = "", timeout: int = 60, stdin: str = "") -> str:
    timeout = max(1, min(int(timeout or 60), 300))
    env = dict(os.environ)
    _fix_display_env(env)
    # 命令里有鼠标点击时，遮罩必须暂时不接事件，否则这点会点在遮罩上变成「用户终止」；
    # 打开后要等遮罩真正让开，否则点击仍会打在还没隐藏的遮罩上
    await _set_overlay_passthrough(bool(_MOUSE_CLICK_RE.search(command)))
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
        proc.kill()
        await proc.wait()
        return (f"命令超时（{timeout}s），已强制终止。\n"
                "提示：若命令需要多次交互输入，请在 stdin 里按行准备好全部答案；"
                "若是长任务，请用 nohup ... > /tmp/日志 2>&1 & 放后台")
    except asyncio.CancelledError:
        # 用户点了终止：杀掉进程并向上传递取消
        proc.kill()
        await proc.wait()
        raise
    finally:
        # 点击已经发完，遮罩恢复接收点击，用户仍可随时点一下终止
        await _set_overlay_passthrough(False)
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
            proc.kill()
            await proc.wait()
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
