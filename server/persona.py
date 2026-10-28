"""DeckMeido 人设：她是谁、怎么称呼你、用什么语气说话

和长期记忆（memory.py）放在一起，两个文件就是完整的「她这个人」：
  meidosoul.json  人设      调换语气、改称呼、换成另一套角色
  memory.json     长期记忆  关于你的事、这台机器上的经验

为什么不放 config.toml：那份文件里是地址、密钥、端口这类运行参数，
人设是会反复调的东西，混在一起既不好改也不好单独备份。

读写都是「读不到/坏掉就用默认值」，不能因为一份人设文件把服务拖挂。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PERSONA_PATH = BASE_DIR / "meidosoul.json"

_lock = threading.Lock()

# 默认人设：和 config.example.toml 里原来的 [persona] 段保持一致，
# 老配置没有这个文件时看到的就是同一套
DEFAULTS = {
    "name": "DeckMeido",
    "traits": "日系女仆式助手：礼貌周到、称用户为主人，办事干练，偶尔带点俏皮的亲近感",
    "extra": "",
    "style": {
        "address": "主人",
        "tone": "礼貌、简洁、干练，偶尔一点俏皮，但不撒娇卖萌",
        "ending": "",
        "suffix": "",
        "forbid": ["喵", "呢~", "人家"],
    },
}

# 各字段的长度上限：人设每轮都会拼进 system prompt，写成长文会挤占上下文
MAX_CHARS = {"name": 40, "traits": 400, "extra": 2000,
             "address": 20, "tone": 400, "ending": 200, "suffix": 40}
MAX_FORBID = 40


def _clean(data: dict) -> dict:
    """按字段白名单整理：未知键丢掉，超长的截断，缺的补默认值"""
    if not isinstance(data, dict):
        data = {}
    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    out = {}
    for key in ("name", "traits", "extra"):
        val = str(data.get(key) or "").strip()
        # extra 是「自由补充设定」，清空就是真的不要了；name/traits 清空则回默认，
        # 否则提示词第一句会变成「你是 ，。」
        if val or key == "extra":
            out[key] = val[:MAX_CHARS[key]]
        else:
            out[key] = DEFAULTS[key]
    forbid = style.get("forbid")
    if not isinstance(forbid, list):
        forbid = DEFAULTS["style"]["forbid"]
    forbid = [str(w).strip()[:MAX_FORBID] for w in forbid if str(w).strip()][:MAX_FORBID]
    out_style = {}
    for key in ("address", "tone", "ending", "suffix"):
        val = str(style.get(key) or "").strip()
        # 称呼留空会变成没头没尾的句子，必须有；其余允许留空
        if not val and key == "address":
            out_style[key] = DEFAULTS["style"][key]
        else:
            out_style[key] = val[:MAX_CHARS[key]]
    out_style["forbid"] = forbid
    out["style"] = out_style
    return out


def load() -> dict:
    """读取人设；文件不存在或坏了就用默认值"""
    try:
        data = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))
    return _clean(data)


def _save(data: dict) -> None:
    """原子写：先写临时文件再替换，断电/杀进程不会留下半截 JSON"""
    data = _clean(data)
    data["updated"] = time.time()
    tmp = PERSONA_PATH.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PERSONA_PATH)


def save(data: dict) -> dict:
    """整体覆盖人设，返回落盘后的实际值（可能被截断过）"""
    with _lock:
        _save(data)
        return load()


def migrate_from_config(cfg: dict) -> bool:
    """老版本的人设写在 config.toml 的 [persona] 段里，这里搬一次

    只在 meidosoul.json 还不存在、而配置里确实有 [persona] 时执行，
    搬完把人设写进 meidosoul.json；config.toml 里的旧段不删（用户自己看着办），
    但从此不再读取它，避免两处都改时互相覆盖。
    """
    if PERSONA_PATH.exists():
        return False
    old = cfg.get("persona") if isinstance(cfg, dict) else None
    if not isinstance(old, dict) or not old:
        return False
    with _lock:
        _save(old)
    return True
