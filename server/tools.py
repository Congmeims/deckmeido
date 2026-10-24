"""DeckMate 工具层：供 LLM function calling 调用的本机能力"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
                },
                "required": ["command", "reason"],
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


async def _run_command(command: str, reason: str = "", timeout: int = 60) -> str:
    timeout = max(1, min(int(timeout or 60), 300))
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(Path.home()),
        env=env,
        executable="/bin/bash",
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"命令超时（{timeout}s），已强制终止"
    text = out.decode("utf-8", "replace").strip()
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


EXECUTORS = {
    "get_weather": _get_weather,
    "web_search": _web_search,
    "list_installed_games": _list_installed_games,
    "find_game": _find_game,
    "check_system_updates": _check_system_updates,
    "install_game": _install_game,
    "list_emulator_roms": _list_emulator_roms,
    "run_command": _run_command,
}


async def execute(name: str, args: dict) -> str:
    fn = EXECUTORS.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return await fn(**args)
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"
