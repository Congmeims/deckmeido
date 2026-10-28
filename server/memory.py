"""DeckMeido 长期记忆：跨会话保留的「关于主人」的条目

和会话历史是两回事：history 是「这次聊了什么」，这里存的是「以后都要知道的」。
按用途分三类，写进同一个 JSON（memory.json，容易被用户直接打开看/备份）：

  profile  用户画像与偏好   「回答控制在三行以内」「别用表情」
  env      环境与操作经验   「启动这个游戏要先开兼容层」
  note     项目与知识条目   「d:/proj 用的是 uv 而不是 pip」

注入方式见 main.py 里的 _memory_block()：拼在 system prompt 的**最末尾**。
记忆会随使用变化，放在开头会让每次改动把后面整段前缀缓存打掉；放在末尾
时受影响的最多只是记忆这一小段。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MEMORY_PATH = BASE_DIR / "memory.json"

# 三类记忆的中文名，注入提示词和前端展示共用
KINDS = {"profile": "用户画像", "env": "环境经验", "note": "知识条目"}
# 单条长度上限：记忆是给模型看的一句话，写成长文会挤占上下文
MAX_ITEM_CHARS = 300
# 总条数上限：超过就按「最近使用的靠前」截断，避免无限膨胀
MAX_ITEMS = 120

_lock = threading.Lock()
# 内存里的「最近注入时间」：只为淘汰时留个依据，不参与每轮写盘
_used_lock = threading.Lock()
_USED: dict[str, float] = {}


def _used_of(it: dict) -> float:
    return max(it.get("used") or 0.0, _USED.get(it.get("id") or "", 0.0))


def _empty() -> dict:
    return {"items": [], "updated": 0.0}


def load() -> dict:
    """读取全部记忆；文件坏了就当没有，不要因为一份记忆文件把服务拖挂"""
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return _empty()
    items = []
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        items.append({
            "id": str(it.get("id") or ""),
            "kind": it.get("kind") if it.get("kind") in KINDS else "note",
            "text": text[:MAX_ITEM_CHARS],
            "created": float(it.get("created") or 0),
            "used": float(it.get("used") or 0),
        })
    data["items"] = items
    return data


def _save(data: dict) -> None:
    """原子写：先写临时文件再替换，断电/杀进程不会留下半截 JSON"""
    data["updated"] = time.time()
    tmp = MEMORY_PATH.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MEMORY_PATH)


def norm(text: str) -> str:
    """去重的比较键：忽略大小写、空白和常见标点差异

    「回答简短一点」和「回答简短一点。」是同一件事，不该存两遍。
    """
    return re.sub(r"[\s。，,.!！?？；;：:、]+", "", str(text)).lower()


def _new_id() -> str:
    return f"m{int(time.time() * 1000):x}{os.getpid() % 1000:03d}"


def add(kind: str, text: str) -> dict:
    """新增一条记忆；内容重复时直接返回已有的那条（不重复存）"""
    kind = kind if kind in KINDS else "note"
    text = str(text or "").strip()[:MAX_ITEM_CHARS]
    if not text:
        return {}
    with _lock:
        data = load()
        key = norm(text)
        for it in data["items"]:
            if norm(it["text"]) == key:
                return it
        item = {"id": _new_id(), "kind": kind, "text": text,
                "created": time.time(), "used": 0.0}
        data["items"].append(item)
        # 超上限时丢最早创建的那些（之前会话里注入过的优先保留）
        if len(data["items"]) > MAX_ITEMS:
            data["items"].sort(key=lambda x: (_used_of(x), x["created"]), reverse=True)
            data["items"] = data["items"][:MAX_ITEMS]
        _save(data)
        return item


def update(mid: str, text: str | None = None, kind: str | None = None) -> bool:
    with _lock:
        data = load()
        for it in data["items"]:
            if it["id"] != mid:
                continue
            if text is not None:
                t = str(text).strip()[:MAX_ITEM_CHARS]
                # 清空正文等于删除这条，前端编辑框清空后保存就是这个意思
                if not t:
                    data["items"] = [x for x in data["items"] if x["id"] != mid]
                    _save(data)
                    return True
                it["text"] = t
            if kind in KINDS:
                it["kind"] = kind
            _save(data)
            return True
        return False


def remove(mid: str) -> bool:
    with _lock:
        data = load()
        left = [x for x in data["items"] if x["id"] != mid]
        if len(left) == len(data["items"]):
            return False
        data["items"] = left
        _save(data)
        return True


def clear() -> None:
    with _lock:
        _save(_empty())


def mark_used(ids: list[str]) -> None:
    """记一次「被注入过」，用于上限淘汰时保留常用的那些

    注入了哪些 id 每个会话都不一样，直接写会每轮都改磁盘上的文件、还要
    和用户编辑记忆抢锁，所以这里只在内存里记一笔，随下一次真正的增删改一起落盘。
    """
    if not ids:
        return
    with _used_lock:
        _USED.update((i, time.time()) for i in ids)


def block(limit: int = 60) -> tuple[str, list[str]]:
    """拼成要注入 system prompt 的文本，返回 (文本, 用到的 id 列表)

    排序固定为「先按类别、再按创建时间」，同一份记忆每次展开结果都一样 —— 
    随机顺序会让每轮前缀都变，缓存命中率直接归零。
    """
    data = load()
    items = sorted(data["items"], key=lambda x: (list(KINDS).index(x["kind"]), x["created"]))
    items = items[:limit]
    if not items:
        return "", []
    lines = ["【关于主人的长期记忆】以下是之前明确要求过要记住的内容，"
             "在相关时遵守，无关时不要强行提起："]
    for kind in KINDS:
        group = [x for x in items if x["kind"] == kind]
        if not group:
            continue
        lines.append(f"{KINDS[kind]}：")
        lines += [f"- {x['text']}" for x in group]
    return "\n".join(lines), [x["id"] for x in items]


def stats() -> dict:
    data = load()
    return {"total": len(data["items"]),
            "profile": sum(1 for x in data["items"] if x["kind"] == "profile"),
            "env": sum(1 for x in data["items"] if x["kind"] == "env"),
            "note": sum(1 for x in data["items"] if x["kind"] == "note")}
