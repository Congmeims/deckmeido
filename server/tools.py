"""DeckMate 工具层：供 LLM function calling 调用的本机能力"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import shutil
from pathlib import Path

import httpx

HTTP = httpx.AsyncClient(timeout=20, follow_redirects=True)

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
            "description": "在设备上执行 bash 命令（通用能力：文件/进程/桌面操作 xdotool/截屏/软件管理等）。环境：SteamOS、桌面模式 X11 DISPLAY=:0、家目录 /home/deck。避免交互式参数，长任务放后台",
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
            "name": "screenshot",
            "description": "截取当前桌面屏幕画面，直接展示给用户。排查问题、用户要求看屏幕时使用",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容（编程/文件任务先看结构）。默认列出工作区 ~/deckmate_workspace",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认 ~/deckmate_workspace"},
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
]

# 危险操作：任何权限模式下都需确认
DANGEROUS = {"install_game"}

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


def _fix_display_env(env: dict):
    """补全桌面会话环境变量：X11/Wayland(Xwayland) 授权文件路径每次开机随机"""
    env.setdefault("DISPLAY", ":0")
    if not env.get("XAUTHORITY"):
        hits = glob.glob("/run/user/1000/xauth_*") or glob.glob("/run/user/1000/.mutter-Xwaylandauth*")
        if hits:
            env["XAUTHORITY"] = hits[0]
        elif (Path.home() / ".Xauthority").exists():
            env["XAUTHORITY"] = str(Path.home() / ".Xauthority")


async def _run_command(command: str, reason: str = "", timeout: int = 60, stdin: str = "") -> str:
    timeout = max(1, min(int(timeout or 60), 300))
    env = dict(os.environ)
    _fix_display_env(env)
    feed = None
    if stdin:
        # 用 script 包一层伪终端：/dev/tty 读取和交互菜单都能收到 stdin 输入
        command = f"script -qefc {shlex.quote(command)} /dev/null"
        feed = (stdin if stdin.endswith("\n") else stdin + "\n").encode()
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE if feed else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(Path.home()),
        env=env,
        executable="/bin/bash",
    )
    try:
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
    text = out.decode("utf-8", "replace").strip()
    if stdin:
        # 伪终端会回显输入，把疑似密码的行打码（短输入如菜单选项保留）
        for line in stdin.splitlines():
            if len(line.strip()) >= 4:
                text = text.replace(line.strip(), "***")
    if len(text) > 4000:
        text = text[:4000] + "\n…（输出过长已截断）"
    return f"exit={proc.returncode}\n{text}" if text else f"exit={proc.returncode}（无输出）"

_CONFIRM_WORDS = {"确认", "好", "好的", "执行", "可以", "yes", "y", "ok"}


def is_confirm(text: str) -> bool:
    return text.strip().lower() in _CONFIRM_WORDS


def describe(name: str, args: dict) -> str:
    """危险操作的人类可读描述"""
    if name == "install_game":
        return f"下载安装 Steam 游戏（appid: {args.get('appid')}）"
    if name == "run_command":
        return f"执行命令：{args.get('command', '')}\n目的：{args.get('reason', '')}"
    return f"执行 {name}({json.dumps(args, ensure_ascii=False)})"


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


async def _screenshot() -> str:
    """截取当前桌面画面（Wayland 优先用 KWin spectacle，X11 退回 mss）"""
    import time

    shots_dir = Path(__file__).resolve().parent / "static" / "shots"
    shots_dir.mkdir(exist_ok=True)
    # 只保留最近 20 张截图
    old = sorted(shots_dir.glob("shot_*.png"), key=lambda p: p.stat().st_mtime)
    for p in old[:-20]:
        p.unlink(missing_ok=True)
    name = f"shot_{int(time.time())}.png"
    path = shots_dir / name

    env = dict(os.environ)
    _fix_display_env(env)
    done = False
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
    if not path.exists():
        return "截图失败：spectacle 与 mss 都不可用"
    return f"截图完成：/shots/{name}（画面已展示给用户；你看不到图，只需告知已截取）"


WORKSPACE = Path.home() / "deckmate_workspace"


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


async def _list_dir(path: str = "") -> str:
    d = _resolve(path) if path else WORKSPACE
    d.mkdir(parents=True, exist_ok=True) if not path else None
    if not d.is_dir():
        return f"不是目录：{d}"
    entries = []
    for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
        tag = "📁" if p.is_dir() else "📄"
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
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p}（{len(content)} 字符）"


async def _edit_file(path: str, old: str, new: str) -> str:
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
    "screenshot": _screenshot,
    "list_dir": _list_dir,
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
}


async def execute(name: str, args: dict) -> str:
    fn = EXECUTORS.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return await fn(**args)
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"
