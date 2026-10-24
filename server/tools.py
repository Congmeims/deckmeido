"""DeckMate 工具层：供 LLM function calling 调用的本机能力"""
from __future__ import annotations

import asyncio
import json
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
]

# 危险操作：执行前需要用户明确确认
DANGEROUS = {"install_game"}

_CONFIRM_WORDS = {"确认", "好", "好的", "执行", "可以", "yes", "y", "ok"}


def is_confirm(text: str) -> bool:
    return text.strip().lower() in _CONFIRM_WORDS


def describe(name: str, args: dict) -> str:
    """危险操作的人类可读描述"""
    if name == "install_game":
        return f"下载安装 Steam 游戏（appid: {args.get('appid')}）"
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


EXECUTORS = {
    "get_weather": _get_weather,
    "web_search": _web_search,
    "list_installed_games": _list_installed_games,
    "find_game": _find_game,
    "check_system_updates": _check_system_updates,
    "install_game": _install_game,
}


async def execute(name: str, args: dict) -> str:
    fn = EXECUTORS.get(name)
    if not fn:
        return f"未知工具：{name}"
    try:
        return await fn(**args)
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"
