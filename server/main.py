"""DeckMeido 后端服务：FastAPI + WebSocket 广播 + LLM 对话"""
from __future__ import annotations

import asyncio
import glob
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import tomllib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Header, HTTPException, Depends, UploadFile, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .llm import LLMClient
from . import depcheck, memory, persona, power, singleton, tools, updater
from .session_store import set_title as _set_title
from .session_store import store
from .session_store import SESSIONS_DIR as _sessions_dir
from .tts import TTS

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
STATIC_DIR = BASE_DIR / "static"

# 安装目录（install.sh 会把代码放在这里，并写入 VERSION 文件）
INSTALL_DIR = BASE_DIR.parent
VERSION = updater.current_version()


# ---------- 配置 ----------

def gen_token(length: int = 5) -> str:
    """生成访问令牌：小写字母 + 数字，如 x7k2m。

    去掉了容易看错的 0/o/1/l/i；校验时不分大小写，全大写输入也能连上。
    """
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        shutil.copy(BASE_DIR / "config.example.toml", CONFIG_PATH)
    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # token 留空（用户手动清空）时补一个，并写回配置文件方便查看
    if not cfg["server"].get("token"):
        token = gen_token()
        text = CONFIG_PATH.read_text(encoding="utf-8")
        text = re.sub(r'^token\s*=\s*""', f'token = "{token}"', text, count=1, flags=re.M)
        CONFIG_PATH.write_text(text, encoding="utf-8")
        cfg["server"]["token"] = token
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace('"', '\\"') + '"'


def _toml_set(text: str, section: str, key: str, value) -> str:
    """在 [section] 内改写 key = value，没有这条就追加到该段末尾"""
    # 段内只匹配到下一个 [ 开头的表头为止，所以子表（如 [persona.style]）不会被误改
    sec_pat = re.compile(rf"(^\[{re.escape(section)}\]\n(?:[^\[\n].*\n?)*)", re.M)
    m = sec_pat.search(text)
    kv_line = f"{key} = {_toml_value(value)}"
    if m:
        block = m.group(1)
        if re.search(rf"^{re.escape(key)}\s*=", block, re.M):
            new_block = re.sub(rf"^{re.escape(key)}\s*=.*$", kv_line, block, flags=re.M)
        else:
            new_block = block.rstrip("\n") + "\n" + kv_line + "\n"
        return text[: m.start()] + new_block + text[m.end():]
    return text.rstrip("\n") + f"\n\n[{section}]\n{kv_line}\n"


def update_config(updates: dict):
    """把 {section: {key: value}} 写回 config.toml（保留注释），并重载运行配置

    值为 dict 时当作子表处理（写进 [section.key]），例如 persona.style。
    旧实现一律按标量格式化，嵌套 dict 会被写成一行字符串，把整份配置弄坏。
    """
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for section, kv in updates.items():
        for key, value in kv.items():
            if isinstance(value, dict):
                sub = f"{section}.{key}"
                # 先清掉旧版本把整个子表写成一行字符串留下的 `key = "{...}"`：
                # 它会让 [section.key] 表头变成「重复定义」，整个配置解析失败。
                # 只在父段内删 —— 全文替换会误伤子表里的同名键。
                # 注意父段里如果已经有 [section.key] 子表，规则会在表头处停下。
                sec_m = re.search(rf"^\[{re.escape(section)}\]\n(?:[^\[\n].*\n?)*", text, re.M)
                if sec_m is None:
                    # 没有父段就整段现建，之后直接写子表头
                    text = text.rstrip("\n") + f"\n\n[{section}]\n"
                elif re.search(rf"^{re.escape(key)}\s*=", sec_m.group(0), re.M):
                    cleaned = re.sub(rf"^{re.escape(key)}\s*=.*\n", "", sec_m.group(0),
                                     count=1, flags=re.M)
                    text = text[: sec_m.start()] + cleaned + text[sec_m.end():]
                # 子表不存在时先建一个空表头，下面逐项往里写
                if not re.search(rf"^\[{re.escape(sub)}\]\s*$", text, re.M):
                    text = text.rstrip("\n") + f"\n\n[{sub}]\n"
                for sub_key, sub_value in value.items():
                    text = _toml_set(text, sub, sub_key, sub_value)
            else:
                text = _toml_set(text, section, key, value)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    apply_config(tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")))


CFG = load_config()
TOKEN = CFG["server"]["token"].strip().lower()

# 单实例守卫：必须在 init_sessions() 之前跑。init_sessions 在导入期就会
# 读/写会话文件，如果本进程注定绑定端口失败却先把内存里的空历史写了盘，
# 正在服务的那一份就会「丢记录」。
# 端口来源优先级：环境变量 > 配置，和 uvicorn 命令行保持一致。
_PORT_EARLY = os.environ.get("DECKMEIDO_PORT") or CFG["server"].get("port", 18765)
singleton.guard(
    int(_PORT_EARLY),
    allow_second=bool(os.environ.get(singleton.ENV_BYPASS)),
)

llm = LLMClient(
    base_url=CFG["llm"]["base_url"],
    api_key=CFG["llm"]["api_key"],
    model=CFG["llm"]["model"],
    timeout=CFG["llm"].get("timeout", 60),
    max_tokens=int(CFG["llm"].get("max_tokens", 4096)),
    # 留空则完全不传，交由服务商默认行为
    reasoning_effort=str(CFG["llm"].get("reasoning_effort", "high")),
)
# 地址填成 DeepSeek（含手抄的 /v1）时把模型名归一，免得带着 gpt-* 去请求
llm.normalize()

PERSONA = persona.load()
# 老版本的人设写在 config.toml 的 [persona] 段，第一次启动时搬进 meidosoul.json，
# 之后再改设置页，人设也只落 meidosoul.json，不再写 config.toml。
# 迁移必须在 load_config 之后、拼 SYSTEM_PROMPT 之前。
if persona.migrate_from_config(CFG.get("persona")):
    PERSONA = persona.load()
# 「截屏询问」的提示词模板。插件点按钮时会把这段连同当前画面发过来，
# 所以模板本身放在配置里，用户可以在设置页随时改（例如换个问法、加一条自己的要求）。
DEFAULT_ASK_TEMPLATE = (
    "看我卡在哪一步，说说现在这个画面里我该做什么。"
    "已经能从上图判断就不要搜索；最多搜 2 次，得到一个答案就停。\n"
    "再总结成几条可以直接照做的步骤，200 字以内。\n"
    "不要剧透：只讲眼前这一步怎么过，不要提前说后面的剧情、结局或其他关卡的解法。"
)
ASK_TEMPLATE = str(CFG.get("agent", {}).get("ask_template") or "").strip() or DEFAULT_ASK_TEMPLATE
# 单轮最多几轮工具往返。以前默认 0 = 不限，模型一旦陷进「截图 → 没看懂 → 再截图」
# 这种循环就会一直跑下去，只能靠用户点终止。12 步对稍微复杂一点的活儿（找文件、
# 改代码、跑测试、修错）往往不够，走到一半就被截断；现在放到 50 步，
# 到顶时仍按既有逻辑补一句「到了单轮上限」，把控制权交回用户。
DEFAULT_MAX_TOOL_ROUNDS = 50
MAX_TOOL_ROUNDS = int(CFG["agent"].get("max_tool_rounds", 0)) or DEFAULT_MAX_TOOL_ROUNDS

# 上下文窗口（真实模型支持的总 token 数）。配 0 表示自动按模型名推断。
# 用量优先取服务商返回的 usage.prompt_tokens，拿不到再按字符估算。
CONTEXT_WINDOW = int(CFG["agent"].get("context_window", 0))
COMPACT_RATIO = float(CFG["agent"].get("compact_ratio", 0.95))
# 压缩完后，最近保留的内容大约占窗口的这个比例。0.3 ≈ 压缩掉七成，
# 保证压一次能撑很久，不会「刚压完又满」——这是压缩频率过高的根源。
COMPACT_TARGET_LEFT = float(CFG["agent"].get("compact_target_left", 0.3))
# 单次输出上限（0 / 留空 = 用满上下文剩余空间）。默认跟随上下文窗口，
# 这样长答复不会再被 4096 这种固定值硬截断。
MAX_OUTPUT_TOKENS = int(CFG["llm"].get("max_output_tokens", 0) or 0) or CONTEXT_WINDOW
# 撞到输出上限时最多自动续写几轮（0 = 不续写）
MAX_CONTINUATIONS = int(CFG["llm"].get("max_continuations", 3))
# 最近一次请求的 token 用量：由服务商 usage 或字符估算更新
_ctx_used = 0


def _guess_context_window(model: str) -> int:
    """按模型名推断上下文窗口，识别不出时给个保守值"""
    m = (model or "").lower()
    table = [
        ("gemini", 1_000_000),
        ("claude", 200_000),
        ("gpt-4.1", 1_000_000),
        ("gpt-4o", 128_000),
        ("gpt-4", 128_000),
        ("o1", 200_000), ("o3", 200_000), ("o4", 200_000),
        ("qwen-long", 10_000_000),
        ("qwen", 128_000),
        ("kimi", 256_000),
        ("glm-4.6", 200_000), ("glm-4.5", 128_000), ("glm", 128_000),
        ("deepseek", 128_000),
        ("llama", 128_000),
        ("mistral", 128_000),
    ]
    for key, size in table:
        if key in m:
            return size
    return 128_000


def context_window() -> int:
    return CONTEXT_WINDOW or _guess_context_window(llm.model)


# ---------- 上下文压缩游标（关键：只压缩「发给模型的上下文」，绝不删界面历史） ----------
# 旧实现把 history 原地替换成「摘要 + 最近 4 条」，compress 完还立刻落盘，
# 于是用户屏幕上已有的几十轮对话被一起抹掉——表现就是「内容不见了」。
# 现在改成游标模型：
#   history        永远只追加，完整记录 = 界面显示 + 磁盘存档
#   store.compact_map  每个会话一份 {"summary": 旧对话摘要, "upto": 摘要覆盖到第几条}
# 发给模型时 = 摘要(system) + history[upto:]，History 本身一条都不动。
# 游标放在 session_store 里，落盘时能一起写进会话文件（见 session_store.write_session）


def _compact_for(sid: str) -> dict:
    return store.compact_for(sid)


def _set_compact(sid: str, summary: str, upto: int) -> None:
    store.set_compact(sid, summary, upto)


# 历史图片的占位文案：固定不变，既省掉每轮重发的几 MB 输入，也保证前缀稳定
_STALE_IMAGE_NOTE = "（这里原本是一张画面，属于更早的对话，已不再发送）"


def _digest_text(m: dict) -> str:
    """把一条历史消息拍平成摘要用的纯文本

    多模态消息的 content 是 list，直接 str() 会把 base64 也塞进摘要请求，
    一次压缩就能发出几十万字符（还全是噪音）。
    """
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(str(p.get("text") or "[图片]") for p in content
                           if isinstance(p, dict))
    return str(content or "")


def _build_llm_msgs(hist: list[dict], sys_prompt: str, sid: str = "") -> list[dict]:
    """构造发给模型的 messages：摘要 + 压缩游标之后的原文

    返回的列表长度就是「来自历史的条数」，调用方据此区分本轮新增的临时消息
    （assistant tool_calls / tool 结果），压缩后重建时可以接回去。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    comp = _compact_for(sid)
    upto = min(comp["upto"], len(hist))
    msgs: list[dict] = [{"role": "system", "content": sys_prompt}]
    if upto > 0 and comp["summary"].strip():
        msgs.append({"role": "system",
                     "content": f"【此前对话摘要，供上下文延续】\n{comp['summary']}"})
    for m in hist[upto:]:
        content = m.get("llm") or m.get("content", "")
        if m["role"] == "user":
            content = _to_vision_content(content) if isinstance(content, str) else content
            if isinstance(content, str) and "[SHOT:" in content:
                # 随提问附带的画面：历史里只有一条文件标记，这里现读成图片部件
                content = _shot_ref_content(content) or content
        # 历史里存下来的画面若过大（旧版截的是无损 PNG，一张 12MB），换成占位符。
        # 网关对请求体有上限，超了会被上游截断并返回 503，整轮提问跟着失败；
        # 这些图只是当时那一轮的参考，用一句说明代替即可
        content = _shrink_oversized_images(content)
        # DeepSeek 没有视觉接口，历史里那几张旧画面本来也看不见：
        # 换成固定文案，省输入费的同时让前缀每轮都一模一样
        if llm.is_deepseek and isinstance(content, list):
            content = [_restale_image_part(p) for p in content]
        # 历史里还存了 ts / events 供界面回放，发给模型时只保留 role 和 content
        msgs.append({"role": m["role"], "content": content,
                     "tool_calls": m.get("tool_calls"),
                     "tool_call_id": m.get("tool_call_id")})
    return _drop_orphan_tools(msgs)


# 上一轮终止时，交接说明里最多列几条工具，再多模型也读不出重点
_ABORT_NOTE_MAX = 12


def _aborted_note(hist: list[dict]) -> str:
    """上一轮是被用户终止的话，给模型一条交接说明

    终止时 history 里只留下「已经说出口的正文 + 工具块」，没有 tool 调用与结果，
    模型看到自己的话断在半路，下一句就会接着往下做（用户看到的就是「我终止了
    它还在跑」）。这里把做过什么、没做什么补给它，并明确要求不许续做。

    只在「最后一条是终止的 assistant」时生效：中间那些早已翻篇的终止轮次
    不该在之后每一轮都再念一遍。
    """
    if not hist:
        return ""
    last = hist[-1]
    if last.get("role") != "assistant" or not last.get("aborted_turn"):
        return ""
    done = [t for t in (last.get("tool_log") or []) if isinstance(t, dict)]
    lines = ["用户在上一轮中途点了「终止」，那一轮已经作废。"]
    finished = [t for t in done if not t.get("interrupted")]
    cut = [t for t in done if t.get("interrupted")]
    if finished:
        lines.append("终止前确实已经执行完这几步，不要重做：")
        for t in finished[:_ABORT_NOTE_MAX]:
            name = t.get("name") or ""
            summary = str(t.get("summary") or "").strip()
            lines.append(f"- {name}：{summary}" if summary else f"- {name}")
        if len(finished) > _ABORT_NOTE_MAX:
            lines.append(f"- 还有 {len(finished) - _ABORT_NOTE_MAX} 步，略")
    if cut:
        lines.append("这些是终止时刚开始、没有执行完的：")
        for t in cut[:_ABORT_NOTE_MAX]:
            name = t.get("name") or ""
            summary = str(t.get("summary") or "").strip()
            lines.append(f"- {name}：{summary}" if summary else f"- {name}")
    lines.append(
        "除非用户在最新这句话里明确要求继续（「继续」「接着做」这类），否则一律不要"
        "自行续做上一轮的任何步骤：不要重新执行、不要补做剩下的步骤、也不要再调用"
        "工具。按用户这次说的话正常回答；如果这句话和上一轮无关，就当上一轮没有发生过。"
    )
    return "\n".join(lines)


# 单张图 base64 的上限。网关实测 1600 万字符必挂；给到 200 万字符（约 1.5MB 原图）
# 已经远超正常截图，留足余量的同时保证单个请求体不会把链路撑爆
_MAX_IMG_CHARS = 2_000_000
# 原图字节数的对应上限（base64 大约是原图的 1.37 倍）
_MAX_IMG_BYTES = int(_MAX_IMG_CHARS / 1.37 * 0.9)
# 收图时的压缩门槛，比上限低得多：插件用 gst 抓的是 1280x800 JPEG q90，
# 一张游戏画面就有 1.8MB（base64 之后 240 万字符），稳稳越过上限被换成占位符。
# 而压到 1024 宽 q75 只有 160KB，看图问答根本看不出差别，所以统一在这里压一遍
_IMG_SOFT_BYTES = 300_000


def _shrink_oversized_images(content):
    """把超大的图片部分替换成文字说明，其余部分原样返回"""
    if not isinstance(content, list):
        return content
    out, changed = [], False
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            if len(url) > _MAX_IMG_CHARS:
                out.append({"type": "text", "text": "（这张图太大无法重新发送，已略过）"})
                changed = True
                continue
        out.append(part)
    return out if changed else content


def _restale_image_part(part: dict) -> dict:
    """把历史里的图片换成固定的一句话（DeepSeek 用）

    历史消息里内联的 base64 是最容易让前缀缓存失效的东西：只要某张图在中间
    被压缩、替换或重新编码，它后面的整段历史都跟着失配。更实际的问题是——
    上下文里躺着几张几 MB 的旧画面，每一轮都在为它们重新付输入费，
    而模型真正需要看的只是「当时有过一张画面」。
    这里换成一段不带编号、不带时间的定长文案：同一张图每轮展开的结果都一样，
    前缀才稳；本轮新截的那张（不在历史里）仍然是原图。
    """
    if isinstance(part, dict) and part.get("type") == "image_url":
        return {"type": "text", "text": _STALE_IMAGE_NOTE}
    return part


def _drop_orphan_tools(msgs: list[dict]) -> list[dict]:
    """去掉没有配对的 tool 消息与 tool_calls，保证发出去的消息能通过接口校验

    OpenAI / DeepSeek 都要求：role=tool 的消息必须紧跟在带 tool_calls 的
    assistant 之后，且每个 tool_call_id 都要有对应的结果；反过来 assistant
    的 tool_calls 也必须有结果跟上。压缩游标恰好切在这一对中间，或者某条
    结果没落盘，就会留下半截 —— 接口直接 400，之后整段会话都发不出去。
    宁可丢掉这几条（摘要里还留着结论），也不能让整轮对话失败。
    """
    ids_with_result = {m.get("tool_call_id") for m in msgs if m["role"] == "tool"}
    # DeepSeek 思考模式下，带过工具调用的 assistant 必须把 reasoning_content
    # 原样回传，少一个字段就 400（见官方「思考模式 → 工具调用」）。这里统一补上：
    # 没存过的（切换过服务商、旧会话）就补空字符串
    keep_reasoning = llm.is_deepseek
    out: list[dict] = []
    for m in msgs:
        if m["role"] == "tool":
            # 上一句不是你请求的这次调用（中间被压缩/被插话），结果无处安放
            prev = out[-1] if out else None
            called = {tc.get("id") for tc in (prev or {}).get("tool_calls") or []}
            if m.get("tool_call_id") not in called:
                continue
            content = m.get("content")
            if keep_reasoning and isinstance(content, list):
                content = [_restale_image_part(p) for p in content]
            m = {"role": "tool", "tool_call_id": m["tool_call_id"], "content": content}
            out.append(m)
            continue
        calls = m.get("tool_calls")
        if calls:
            # 只保留真有结果的调用；一个都不剩就把这条 assistant 降级成普通回复，
            # 否则它挂着空的 tool_calls，接口同样不认
            kept = [tc for tc in calls if tc.get("id") in ids_with_result]
            if not kept:
                if not str(m.get("content") or "").strip():
                    continue
                m = {"role": m["role"], "content": m["content"]}
            else:
                m = {**m, "tool_calls": kept}
                if keep_reasoning:
                    m.setdefault("reasoning_content", "")
        else:
            m = {"role": m["role"], "content": m["content"]}
        out.append(m)
    return out



def _estimate_tokens(msgs: list[dict]) -> int:
    """粗估 token：中文约 1 字 1 token，英文约 4 字符 1 token，另加每条消息的固定开销"""
    total = 0
    for m in msgs:
        content = m.get("content")
        # 多模态消息的 content 是 list，图片以 base64 形式挂在里面。直接 str()
        # 会把整串 base64 当文本数：一张 1280x800 的 PNG 有 1600 万字符，
        # 估出来四百多万 token，于是每轮都判定「该压缩了」，上来就把对话压成摘要。
        # 图片按固定额度计：主流模型对一张普通图收 800~1600 token 左右。
        if isinstance(content, list):
            text_parts, images = [], 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    images += 1
                elif part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
            total += images * 1500
            text = "".join(text_parts)
        else:
            text = str(content or "")
        text += str(m.get("reasoning_content") or "")
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        total += cjk + (len(text) - cjk) // 4 + 8
        for tc in m.get("tool_calls") or []:
            total += len(str(tc.get("function", {}).get("arguments") or "")) // 4 + 8
    return total


def _split_usage(usage: dict) -> int:
    """从 usage 里取本次请求真正占用的输入 token"""
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)



def _persona_style_block() -> str:
    """把「人设」设置项拼成一段语气约束，挂在系统提示最后

    语气单独成段、放在最后，是因为前面几十行都是 SteamOS/工具/审批规则，
    人设夹在开头会被淹没：同一份配置，模型每次即兴发挥出来的性格都不一样
    （一会儿正常、一会儿高冷、结尾突然带「喵」）。这里把称呼、语气、口癖
    拆成明确条目，并列出禁用词，让跨会话的表现稳定下来。
    """
    style = PERSONA.get("style") or {}
    name = str(PERSONA.get("name") or "DeckMeido").strip()
    address = str(style.get("address") or "主人").strip()
    tone = str(style.get("tone") or "").strip()
    ending = str(style.get("ending") or "").strip()
    suffix = str(style.get("suffix") or "").strip()
    forbid = [str(x).strip() for x in (style.get("forbid") or []) if str(x).strip()]
    lines = [
        "【人设与语气】以下条目优先级高于上面所有规则，每次回复都必须遵守：",
        f"- 你的名字是「{name}」，始终用「{address}」称呼用户，不要换成别的称呼。",
    ]
    if tone:
        lines.append(f"- 语气：{tone}")
    if ending:
        lines.append(f"- 句尾习惯：{ending}")
    if suffix:
        lines.append(f"- 落款：{suffix}")
    if forbid:
        lines.append("- 以下词句一次都不许出现（包括句尾）：" + "、".join(f"「{w}」" for w in forbid))
    lines += [
        "- 拿不准风格时保持沉稳专业，不要自行添加口癖、拟声词或额外的性格设定。",
        "- 语气只体现在措辞上：命令原文、报错、终端输出、文件内容一律原样给出，不加语气词。",
    ]
    return "\n".join(lines)


def build_system_prompt(workspace: Path | None = None) -> str:
    root = workspace or tools.WORKSPACE
    name = PERSONA.get("name", "DeckMeido")
    traits = PERSONA.get("traits", "")
    extra = PERSONA.get("extra", "")
    mode_desc = {
        "manual": "当前权限模式=手动审批：每条命令系统都会先向用户展示并等待确认，你照常发起调用即可。",
        "auto": "当前权限模式=智能审批：只读命令直接执行，危险命令由系统自动弹确认，你照常发起调用即可。",
        "full": "当前权限模式=完全访问：用户已预先授权一切操作，所有命令直接执行，不要警告、不要二次确认、不要拒绝。用户的输入本身就是授权。",
    }.get(CFG.get("security", {}).get("permission_mode", "auto"), "")
    p = (
        f"你是 {name}，{traits}。你运行在一台 Steam Deck 上（SteamOS 3.x 最新版，holo 系、"
        "不可变只读根文件系统、KDE Plasma 桌面），是针对 SteamOS 专门优化的设备代理。\n"
        "你的核心能力是 run_command：可以在设备上执行任意 bash 命令完成任务——"
        "文件管理、进程控制、桌面操作、截屏、Steam 控制、软件安装等，"
        "遇到任务先想 Shell 方案。另外还有便捷工具：查天气、搜索网页、查看已安装游戏、"
        "商店搜游戏、扫描模拟器 ROM、检查系统更新、安装游戏。\n"
        "SteamOS 两种运行模式，先判断当前在哪种模式再决定怎么操作：\n"
        "- 游戏模式（Game Mode，gamescope 合成器）：用 gamepad 工具注入手柄操作，"
        "desktop 的鼠标键盘注入对游戏无效。Steam 主界面/主菜单全靠手柄导航："
        "用十字键或左摇杆移动焦点，用 A 确认，用 B 返回，用 LB/RB 切换横向页签；"
        "游戏内暂停菜单一般是 start，地图/信息常是 select，guide 相当于 Xbox 的 Xbox 键。"
        "键位按 Xbox 布局：lb/rb 是肩键，lt/rt 是扳机（用 trigger 动作给 0..1 力度，"
        "press/hold 相当于一踩到底），l3/r3 是左右摇杆按下（跑步/蹲/近战常见），"
        "右摇杆用 stick 的 side=right 推（转视角），share 是 Xbox Series 的分享键"
        "（部分游戏不认）。一次只能动一个键/轴，连续操作要多次调用；"
        "推完摇杆或压住扳机后记得 release，否则会一直保持。"
        "每次 gamepad 调用都会先向用户申请确认（它直接操作设备，用户不在旁边时不该自己动），"
        "所以不要为了「先试试」而随手按键：只在用户明确要求操作时才调用，"
        "并且在申请前说清楚你要按什么、为什么。\n"
        "呼出 Steam 的快速访问菜单（QAM，音量/亮度/TDP/性能覆盖/蓝牙）和 Steam 主菜单"
        "是系统层拦物理 Steam 键实现的，虚拟手柄做不到，需要时请用户自己按 Steam 键。\n"
        "- 桌面模式（Desktop Mode，KDE Plasma + Wayland）：用 desktop 工具点按输入，"
        "装软件走 Discovery（Flatpak）而不是 pacman；系统目录只读，别试图改 /usr。"
        "两种模式互切在 Steam 键 → 电源 → 切换到桌面/切换到游戏模式，同样请用户自己按。\n"
        "编程能力：你有自己的工作区（当前：" + str(root) + "）。写代码/改文件用专用工具："
        "list_dir 看结构、read_file 读文件（带行号）、write_file 创建、edit_file 精确替换；"
        "运行测试/脚本再用 run_command。用户让做项目时，先规划再逐步实施，像一个编程搭档。\n"
        "screenshot 工具可截取当前画面展示给用户：排查问题、确认状态或用户要求看屏幕时主动使用"
        "（画面会同时显示在聊天里并附给你，你可以直接看图回答用户问题；"
        "游戏模式从 gamescope 抓、桌面模式走 spectacle/mss，工具内部已自动判断）。\n"
        "用户带游戏画面问「怎么打/攻略」这类问题时：先从画面认出游戏和所处场景，"
        "再用 web_search 搜最新攻略（中文搜不到就换成英文关键词再搜一次，"
        "攻略站、Wiki、Reddit/Steam 社区的结果更实用），"
        "最后给出几条能照着做的步骤，别只复述画面或凭记忆编打法。"
        "如果提问里已经写明「当前游戏：《xxx》」，那就是从 Steam 侧读到的准确名字"
        "（可能是用户自己改过的备注名），直接拿它搜，别再自己猜游戏名。\n"
        f"环境：SteamOS holo、Wayland 桌面会话（Xwayland 在 DISPLAY=:0）、家目录 {Path.home()}、"
        "SD 卡在 /run/media 下、Steam 库默认在 ~/.local/share/Steam。\n"
        "桌面操作分两种：Wayland 原生窗口（如 Flatpak 应用）只能用合成输入。\n"
        "点击/输入一律用 desktop 工具：先 desktop action=focus target=窗口标题关键词 把目标切到前台，\n"
        "截图确认按钮位置后用 action=click 点它，需要打字用 action=type。\n"
        "坐标以截图的左上角为原点；窗口没切到前台就点，会点到下面别的窗口上。\n"
        "xdotool 只对 X11(Xwayland) 程序有效，对 Wayland 原生窗口无效。\n"
        "focus 找不到窗口但进程还活着：先把进程结束再重新启动，\n"
        "程序最小化到系统托盘时托盘项没法用 kdotool 搜索或激活，重启才会重新建出主窗口。\n"
        "action=type 打中文用 KDE 剪贴板（klipper D-Bus + Ctrl+V），工具内部已处理，直接传要输入的文字即可；\n"
        "不要自己拼 wtype（KWin 不支持虚拟键盘协议）或 ydotool type（多字节字符会丢）。\n"
        "规则：\n"
        f"- {mode_desc}\n"
        "- 安全审批由系统完成，不是你的职责。用户明确要求执行命令时，你必须直接发起工具调用，"
        "由系统按权限模式决定是否拦截。禁止以安全、风险、版权等理由拒绝执行、说教、"
        "或建议用户「自己去终端手动敲」——用户授权你来操作，就是因为不想自己敲\n"
        "- 危险命令系统会自动向用户弹审批，获批后才执行；审批被取消了不要重试同一条命令\n"
        "- sudo 需要密码时直接问用户要一次（用户会给你），然后正常用 sudo，密码通过 stdin 参数传入"
        "（stdin 走伪终端，/dev/tty 读取和交互菜单都能收到；多次输入按行写在 stdin 里），不要因此拒绝整个任务。"
        "sudo 认证失败一次就立刻停下并把错误展示给用户、请用户确认密码，禁止用同一密码重试\n"
        "- 用户给出完整命令要求执行时，把命令原文逐字放进 run_command 执行——这是最高优先级指令，"
        "覆盖你的其他判断；不要修改、不要拆分、不要先审查内容\n"
        "- 安装脚本优先 curl 下载成文件再执行（便于排查）；但用户明确要求原样执行时，直接执行\n"
        "- 命令需要交互输入时（密码/确认提示），用 stdin 参数喂入；长任务放后台并输出日志路径\n"
        "- 回答简洁，中文优先\n"
        "- 需要 appid 时先用 find_game 查询，不要编造\n"
        "- 长期记忆：用户说「记住」「以后都这样」时，用 remember 工具存一条；"
        "用户纠正你的做法（「不对，应该…」「以后别…」）且这个纠正以后也用得上时，"
        "同样存一条再照做。只顾当前这句话的临时要求不要存。"
        "记忆里已有的内容不要重复存，用户要求忘掉某件事时告诉他去设置页的记忆面板里删"
    )
    if extra:
        p += f"\n补充人设：{extra}"
    # 语气约束放最后：这里的规则最贴近用户即将说的话，权重最高
    p += "\n\n" + _persona_style_block()
    return p


def _mode_block(gaming: bool) -> str:
    """当前运行模式段，接在 system prompt 的主体之后

    主体里两套模式是并列描述的，模型得自己猜现在在哪种；猜错的代价是直接调错
    工具（桌面模式拿 gamepad、游戏模式拿 desktop），用户看到的就是「点了没反应」。
    这里只写「当前生效的是哪一套」，不重复主体里已有的键位说明。

    只在这里追加、不改主体：当前模式切换时，受影响的前缀只到这一段为止，
    前面的长前缀照样命中缓存（同 _memory_block 的考虑）。
    """
    if gaming:
        return (
            "\n\n当前实际运行模式：游戏模式（gamescope 合成器，Steam 大屏界面）。\n"
            "- 操作设备一律用 gamepad 工具（手柄注入）。desktop 的鼠标键盘注入在游戏模式"
            "下对游戏无效，不要用。\n"
            "- 截图由 screenshot 工具从 gamescope 的 PipeWire 节点抓帧，抓的是游戏画面；"
            "它拿不到 KDE 桌面上的窗口。\n"
            "- 遮罩/提示这类画面反馈由系统挂在 gamescope 的覆盖层上，你正常调用工具即可，"
            "不要自己去画。\n"
            "- QAM 和 Steam 主菜单是系统层拦物理 Steam 键实现的，虚拟手柄做不到，"
            "需要时请用户自己按 Steam 键。\n"
            "- 不要试图用 xdotool / kdotool 操作游戏窗口，也不要建议用户切到桌面去点。"
        )
    return (
        "\n\n当前实际运行模式：桌面模式（KDE Plasma + Wayland，非 gamescope）。\n"
        "- 操作设备一律用 desktop 工具（focus 切前台 → 截图 → click/type）。"
        "gamepad 在桌面模式下没有意义，不要用。\n"
        "- 截图由 screenshot 工具走 spectacle/mss，抓的是当前桌面；不存在 gamescope "
        "画面可抓，也别建议用户切到游戏模式去截图。\n"
        "- 遮罩/提示走的是桌面通知/窗口这一套，不要按游戏模式的覆盖层去理解。\n"
        "- Wayland 原生窗口只认合成输入；xdotool 仅对 X11(Xwayland) 程序有效。"
    )


def _memory_block() -> str:
    """长期记忆段，接在 system prompt 最末尾

    必须放在最末尾而不是开头：记忆会随使用增删，放在前面的话，只要加一条，
    后面整段几十行前缀就被打掉，DeepSeek 的缓存命中率会直接掉到 0。
    放在末尾时，一次记忆变动影响的只是这一小段之后的缓存（本来就没什么内容）。
    单条记忆每轮都按「类别 + 创建时间」稳定排序，同样的记忆展开结果一字不差。
    """
    text, ids = memory.block()
    if ids:
        # 只记「谁被注入过」，供记忆条数到上限时优先保留常用的那些
        memory.mark_used(ids)
    return text


SYSTEM_PROMPT = build_system_prompt()

_tts_cfg = CFG.get("tts", {})
tts = TTS(
    enable=bool(_tts_cfg.get("enable", False)),
    engine=_tts_cfg.get("engine", "espeak-ng"),
    voice=_tts_cfg.get("voice", "cmn"),
    speed=int(_tts_cfg.get("speed", 170)),
    max_chars=int(_tts_cfg.get("max_chars", 300)),
)


def apply_config(cfg: dict):
    """热更新运行配置（LLM / TTS / 权限模式 / 访问令牌），无需重启"""
    global CFG, TOKEN, PERMISSION_MODE, PERSONA, SYSTEM_PROMPT, MAX_TOOL_ROUNDS, ASK_TEMPLATE
    CFG = cfg
    # 人设不在这里取：它存在 meidosoul.json，改完由 update_config 的调用方
    # 直接 reload_persona()，config.toml 里即使还留着旧的 [persona] 段也不再生效
    TOKEN = (cfg["server"].get("token") or TOKEN).strip().lower()
    MAX_TOOL_ROUNDS = (int(cfg.get("agent", {}).get("max_tool_rounds", 0))
                       or DEFAULT_MAX_TOOL_ROUNDS)
    ASK_TEMPLATE = (str(cfg.get("agent", {}).get("ask_template") or "").strip()
                    or DEFAULT_ASK_TEMPLATE)
    c = cfg["llm"]
    llm.base_url = c["base_url"].rstrip("/")
    llm.api_key = c["api_key"]
    llm.model = c["model"]
    llm.reasoning_effort = str(c.get("reasoning_effort", "high"))
    llm.normalize()
    t = cfg.get("tts", {})
    was_enabled = tts.enable
    tts.enable = bool(t.get("enable", False))
    # 关掉播报时立刻掐断正在念的那句，不能等它自己读完。
    # apply_config 也可能在启动阶段（没跑事件循环）被调用，那时没有 loop 可建任务
    if was_enabled and not tts.enable:
        try:
            asyncio.get_running_loop().create_task(tts.stop())
        except RuntimeError:
            pass
    tts.voice = t.get("voice", "cmn")
    tts.speed = int(t.get("speed", 170))
    PERMISSION_MODE = cfg.get("security", {}).get("permission_mode", "auto")
    # 文件工具的边界跟着权限模式走：full 不设限（与 run_command 一致），
    # 其余模式只能碰工作区。这里同步一份，热更新后立刻生效
    tools.path_bound = PERMISSION_MODE != "full"
    # 桌面模式下执行命令/亮遮罩时阻止息屏（热更新即生效，不必重启）
    wake_on = cfg.get("power", {}).get("keep_awake", True)
    power.keeper.enabled = bool(wake_on)
    if not power.keeper.enabled:
        power.keeper.release_all()
    ws_dir = cfg.get("workspace", {}).get("dir", "")
    if ws_dir:
        try:
            tools.set_workspace(ws_dir)
        except Exception:
            pass
    SYSTEM_PROMPT = build_system_prompt()


def reload_persona() -> dict:
    """重新读 meidosoul.json 并重拼系统提示词，保存后立即生效

    人设存在独立文件里，不走 update_config / apply_config 那条热更新路径，
    由设置页和导入接口直接调用这里。
    """
    global PERSONA, SYSTEM_PROMPT
    PERSONA = persona.load()
    SYSTEM_PROMPT = build_system_prompt()
    return PERSONA


# 权限模式：manual=所有命令手动审批 / auto=危险命令才审批 / full=完全访问
PERMISSION_MODE = CFG.get("security", {}).get("permission_mode", "auto")
tools.path_bound = PERMISSION_MODE != "full"

# 工作区（默认 ~/deckmeido_workspace，可在设置里改）
DEFAULT_WORKSPACE = Path.home() / "deckmeido_workspace"
try:
    tools.set_workspace(CFG.get("workspace", {}).get("dir") or str(DEFAULT_WORKSPACE))
except Exception:
    pass

# ---------- 会话持久化 ----------
# 状态与落盘都在 session_store 里（store.current_sid / store.history / store.turn ...）。
# 这里保留原来的名字，调用点不用改；但这些都是「读 store」的视图，不是第二份状态。

SESSIONS_DIR = _sessions_dir


# 会话文件的写入锁，见 session_store.SessionStore.lock
_SESS_WRITE_LOCK = store.lock


def _turn_detached(epoch: int) -> bool:
    """这轮开始时快照的 epoch 是否已过期（会话被切走或重建）"""
    return store.turn_detached(epoch)


def _turn_tag() -> dict:
    """给广播消息挂上本轮所属会话号；前端据此丢弃不属于当前会话的流式片段。

    发起端自带的 turn_id 也一并带上：插件要认出「哪些 δ 是我这一轮的」，
    只看 sid 不行——同一会话里别的端提问也是这个 sid。
    """
    return store.turn_tag()


def _sess_path(sid: str) -> Path:
    return store.sess_path(sid)


def save_current_session():
    """把当前会话历史写盘，标题取第一条用户消息"""
    store.save_current()


def _fresh_sid() -> str:
    """生成一个目录里绝对不存在的会话号（实现见 session_store）"""
    return store.fresh_sid()


def save_session_snapshot(sid: str, hist: list[dict]):
    """把指定快照写回指定会话文件（会话中途被切走时，用这招把结果送回原会话）"""
    return store.save_snapshot(sid, hist)


def _write_session(sid: str, hist: list[dict]):
    """原子写入一个会话文件（落盘策略见 session_store.write_session）"""
    store.write_session(sid, hist)


# ---------- 流式正文的增量落盘 ----------
# 正文原本是「整轮生成完才落盘」的，而一轮回答可能长达几分钟；这期间进程被杀
# （客户端退出、升级、OOM）就整段消失，用户看到的就是「我的回复不见了」。
# 下面在流式回调里按节流频率把半截正文写进会话文件的 _streaming 字段，
# 进程被杀最多丢 1 秒；下次启动 init_sessions 会把它恢复成「回答中断」的消息。

_STREAM_FLUSH_INTERVAL = 0.6   # 秒（越短丢得越少；写的是小文件，代价可接受）
_STREAM_FLUSH_CHARS = 200      # 或自上次落盘以来又累积这么多字
_RESTORE_MIN_CHARS = 0         # 恢复门槛已取消：一个字也救回来，见 init_sessions
# events 槽：本轮到目前为止「屏幕上已经出现过」的有序内容块。
# 只存最后那段正文的话，一轮里先说话、再调工具、再说一段的，中途被杀就只剩最后一段
# 按 sid 各存一份：两轮并发（不同会话）时共用单槽会互相覆盖 —— A 的正文被 B 清掉，
# 终止时 A 交给下一轮的交接正文就空了；收尾时还会把 B 的正文写进 A 的文件
_stream_bufs: dict[str, dict] = {}


def _stream_buf(sid: str = "") -> dict:
    """取某个会话的流式缓冲（没有就建一个空的）"""
    sid = sid or store.turn.get("sid") or store.current_sid
    buf = _stream_bufs.get(sid)
    if buf is None:
        buf = {"sid": sid, "text": "", "last": 0.0, "written": 0, "parts": []}
        _stream_bufs[sid] = buf
    return buf


def _write_streaming(sid: str, text: str, parts: list[dict] | None = None) -> None:
    """把半截正文写进会话文件的 _streaming 字段（不动 history）

    parts 是「屏幕上已经出现过的全部内容」的有序块列表：
      {"type":"text","content":...}  ← 助手正文段
      {"type":"event","event":{...}} ← 思维链 / 工具时间线
    只存 text 的话，一轮里先说话、再调工具、再说一段的，中途被杀就只剩最后一段。
    """
    with store.lock:
        if sid in store.deleted_sids:
            return
        p = store.sess_path(sid)
        data: dict = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.setdefault("id", sid)
        data.setdefault("title", "新会话")
        data.setdefault("created", time.time())
        data.setdefault("history", [])
        _set_title(data, data.get("history") or [])
        data["updated"] = time.time()
        data["_streaming"] = {"text": text, "ts": time.time(),
                              "parts": list(parts or [])}
        # 临时文件名带 pid：万一还有别的进程（旧的孤儿后端）在写同一份会话，
        # 至少不会互相踩坏对方正在写的临时文件
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass


def persist_stream(text: str, force: bool = False, parts: list[dict] | None = None,
                   sid: str = "") -> None:
    """流式正文的节流落盘；force=True 立即写（终止 / checkpoint / 收尾时用）

    sid 不传时按本轮归属推断；调用方最好显式传，避免跨会话串写。
    parts 传入时同时更新缓冲区里「屏幕上出现过的内容」有序块列表，
    这样即便只存了最后一段正文，恢复时也能按原顺序把所有段落和事件拼回来。
    """
    sid = sid or store.turn.get("sid") or store.current_sid
    buf = _stream_buf(sid)
    buf["text"] = text
    if parts is not None:
        buf["parts"] = parts
    now = time.time()
    if not force:
        grew = len(text) - buf["written"]
        if (now - buf["last"]) < _STREAM_FLUSH_INTERVAL and grew < _STREAM_FLUSH_CHARS:
            return
    buf["last"] = now
    buf["written"] = len(text)
    _write_streaming(sid, text, buf.get("parts") or [])


def _snapshot_parts(events: list[dict], tail_text: str = "") -> list[dict]:
    """把本轮事件与当前正文拼成「屏幕上出现过的内容」有序块列表

    事件的 text 段（工具调用前的过渡正文）按顺序穿插在思维链/工具块之间，
    尾部的 tail_text 是最后一个事件之后正在进行的那段正文。
    """
    parts: list[dict] = []
    for ev in events or []:
        if ev.get("type") == "text":
            parts.append({"type": "text", "content": ev.get("text") or ""})
        else:
            parts.append({"type": "event", "event": ev})
    if tail_text:
        parts.append({"type": "text", "content": tail_text})
    return parts


def clear_stream(sid: str = "") -> None:
    """正文已正式落盘，把残留的 _streaming 字段清掉"""
    sid = sid or store.turn.get("sid") or store.current_sid
    _stream_bufs.pop(sid, None)
    with store.lock:
        if sid in store.deleted_sids:
            return
        data = store.read_session(sid)
        if not data:
            return
        if not data.pop("_streaming", None):
            return
        store.rewrite_session(sid, data)


_UPLOAD_RE = re.compile(r"!\[[^\]]*\]\((/uploads/[^\s)]+\.(?:png|jpe?g|webp))\)", re.I)
# 随提问附带、已经落成本地文件的画面：文字里留一行标记，发给模型时按文件现读现编码。
# 历史里因此不再躺着几 MB 的 base64 —— 那是最容易被裁掉的东西，一裁图就没了
_SHOT_REF_RE = re.compile(r"\[SHOT:([^\]\s]+)\]")


def _shot_ref_path(name: str) -> Path | None:
    fname = Path(name).name
    if not name or fname != name:
        return None
    fp = STATIC_DIR / "shots" / fname
    return fp if fp.is_file() else None


def _to_vision_content(text: str):
    """消息里含上传图片引用时转成多模态 content（供 vision 模型看图）"""
    matches = _UPLOAD_RE.findall(text)
    if not matches:
        return text
    parts = []
    for url in matches:
        fp = STATIC_DIR / url.lstrip("/")
        if not fp.is_file():
            continue
        import base64
        ext = "jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else fp.suffix.lstrip(".").lower()
        b64 = base64.b64encode(fp.read_bytes()).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}})
    if not parts:
        return text
    parts.append({"type": "text", "text": _UPLOAD_RE.sub("[图片]", text)})
    return parts


def _shot_caption(seq: int) -> str:
    """给随提问附带的画面配一句说明

    同一条会话里连着截几次，上下文里就并排躺着好几张图，文案还一字不差。
    模型分不清哪张对应当前这一问，经常拿着上一张回答（用户看到的就是
    「截的是游戏画面，答的还是几分钟前的 Steam 菜单」）。所以给这张编号，
    并明确告诉它只看最新这张，前面的都已过时。

    定义位置必须早于 _shot_ref_content：init_sessions() 在模块导入时就跑，
    它会走 _build_llm_msgs → _expand_context → _shot_ref_content，
    若本函数定义在 init_sessions 之后，恢复历史时就是 NameError。
    """
    if seq <= 1:
        return "（这是本次提问时截取的当前画面，请依据这张图回答）"
    return (f"（这是本次提问时截取的当前画面，第 {seq} 张。"
            "前面的画面都是更早的提问，已经过时；请只依据这一张回答）")


def _shot_ref_content(text: str):
    """把文字里的 [SHOT:xxx.jpg] 标记换成图片部件

    发给模型的图只在这一步读，历史里存的是一条几十字节的标记：
    之前库里直接存着 base64，一旦被上下文压缩裁掉，界面上的图也跟着没了，
    而且模型看到的那条内容每次都要重新编码，前缀缓存也用不上。
    """
    names = _SHOT_REF_RE.findall(text or "")
    if not names:
        return None
    parts: list[dict] = []
    # 说明放在图片前面：它在拼接位置上是稳定前缀，后面的历史才命中得了前缀缓存；
    # 顺带让模型先知道「这张才是当前画面，前面那些已过时」
    parts.append({"type": "text", "text": _shot_caption(len(names))})
    for name in names:
        fp = _shot_ref_path(name)
        if not fp:
            continue
        import base64
        ext = "jpeg" if fp.suffix.lower() in (".jpg", ".jpeg") else fp.suffix.lstrip(".").lower()
        b64 = base64.b64encode(fp.read_bytes()).decode()
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/{ext};base64,{b64}"}})
    if len(parts) == 1:
        return None
    # 标记本身留在正文里，和上传图片的处理方式一致：模型知道这张图对应哪句话
    parts.append({"type": "text", "text": text})
    return parts


def init_sessions():
    """启动时恢复最近的会话；没有历史则创建默认会话"""
    global _ctx_used
    files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        save_current_session()
        return

    # 逐个试：以前只认 mtime 最新的那一个，它坏了就 history.clear() 整个重来，
    # 几十轮历史当场归零。现在坏一个就跳过试下一个，全坏才开新会话。
    # 注意要排除「空会话文件」：空文件不是损坏，但拿它当恢复结果同样等于清空，
    # 所以空 history 的文件也当作不合格，继续往旧文件里找。
    def _restore(data: dict, src: Path) -> bool:
        """从一个会话文件恢复出完整状态；成功返回 True"""
        global _ctx_used
        sid = data.get("id") or src.stem
        hist = data.get("history") or []
        if not isinstance(hist, list) or not hist:
            return False
        store.current_sid = sid
        store.history = hist
        comp = data.get("compact")
        if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
            _set_compact(sid, comp.get("summary") or "",
                         min(int(comp.get("upto") or 0), len(store.history)))
        else:
            store.drop_compact(sid)
        # 上一轮回答被打断：把当时流式落盘的半截正文恢复成一条「回答中断」的消息，
        # 否则用户只看到自己的提问，像是回答凭空消失了
        partial = data.pop("_streaming", None)
        partial_text = (partial.get("text") or "").strip() if isinstance(partial, dict) else ""
        partial_parts = (partial.get("parts") or []) if isinstance(partial, dict) else []
        # 恢复门槛已取消：只要落盘过半个字，就恢复成一条「回答中断」的消息。
        # 之前设了 30 字门槛（想避免「好的」这种两个字占一条消息），代价是
        # 用户看到的现象变成「内容凭空消失」——而被吃掉的往往正是开头那段
        # 最有信息量的结论。宁可多一条带「已中断」标记的短消息，也不能丢内容。
        if partial_parts:
            # 分块恢复：屏幕上出现过的每一段正文都按原顺序保留，事件（思维链/工具）挂到
            # 同一条消息的 events 上。旧版只存最后一段正文，前几句过渡话会丢，这里已修
            segs = [(p.get("content") or "") for p in partial_parts if p.get("type") == "text"]
            evs = [p.get("event") for p in partial_parts if p.get("type") == "event"]
            partial_text = "\n\n".join(s.strip() for s in segs if s.strip()).strip()
        else:
            evs = []
        # 本轮最后的正文若比 parts 里更全（流式尾部还没进 parts），补在末尾
        tail_text = (partial.get("text") or "").strip() if isinstance(partial, dict) else ""
        if tail_text and tail_text not in partial_text:
            partial_text = (partial_text + "\n\n" + tail_text).strip() if partial_text else tail_text
        if partial_text or evs:
            note = "\n\n（以上回答被中断，已保留屏幕上出现过的全部内容）"
            store.history.append({"role": "assistant",
                                  "content": (partial_text.rstrip() + note) if partial_text else note.strip(),
                                  "events": evs, "interrupted": True,
                                  "ts": (partial.get("ts") if isinstance(partial, dict) else None) or time.time()})
            print(f"[DeckMeido] 已恢复被中断的回答（{len(partial_text)} 字，{len(evs)} 个事件）",
                  file=sys.stderr)
        _ctx_used = _estimate_tokens(_build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
        if partial is not None:
            # 无论是「恢复成消息」还是「判定太短丢弃」，都要把临时字段抹掉后落盘：
            #   - 不落盘：恢复出来的内容后端再死一次又没了
            #   - 不抹字段：文件上的 _streaming 还在，下次启动再恢复一遍，
            #     同一条回答会越重启越多
            try:
                _write_session(store.current_sid, store.history)
            except Exception as e:
                print(f"[DeckMeido] 恢复中断回答后落盘失败：{e}", file=sys.stderr)
        print(f"[DeckMeido] 已从 {src.name} 恢复会话 {store.current_sid}（{len(store.history)} 条）",
              file=sys.stderr)
        return True

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            # 单个文件损坏：留个备份，继续试下一个，别让一个坏文件把历史全毁了
            print(f"[DeckMeido] 会话文件 {f.name} 解析失败（{e}），已留档并尝试下一个",
                  file=sys.stderr)
            try:
                f.rename(f.with_name(f.name + ".broken"))
            except OSError:
                pass
            continue
        try:
            if _restore(data, f):
                return
        except Exception as e:
            print(f"[DeckMeido] 从 {f.name} 恢复失败（{e}），尝试下一个", file=sys.stderr)
            continue

    # 全部不可用：开一个全新会话顶上（换随机 sid，避免和目录里残存的旧文件撞号）
    store.current_sid = _fresh_sid()
    store.history = []
    _ctx_used = 0
    save_current_session()


_load_initial_sessions = init_sessions
_init_sessions = init_sessions
_init_sessions()

# 消息 feed（供原生客户端轮询同步，增量拉取）
feed: list[dict] = []
_feed_seq = 0


def feed_push(type_: str, **kw):
    global _feed_seq
    _feed_seq += 1
    feed.append({"id": _feed_seq, "type": type_, "ts": time.time(), **kw})
    del feed[:-200]  # 最多保留 200 条

# 待确认的危险操作 {name, args, description, sid, epoch, turn_id, client, device}
# sid/epoch/client/device 是「谁在什么会话里提的」：确认只认原会话原来源，
# 否则任何连上的客户端随便一句「好」就能放行别人发起的危险命令
pending_action: dict | None = None


def _pending_matches(p: dict, sid: str, epoch: int,
                     client: str | None, device: str | None) -> bool:
    """这条待确认操作是不是本轮发起者自己提的"""
    if p.get("sid") != sid or p.get("epoch") != epoch:
        return False
    # 来源也一起比：同一会话里换了个端发「好」不算确认（网页和插件能同时连）
    return (p.get("client") or "") == (client or "") \
        and (p.get("device") or "") == (device or "")


def _pending_matches_turn(p: dict, sid: str, epoch: int, turn_id: str,
                          client: str | None, device: str | None) -> bool:
    """这个端的这一轮，能不能认领并确认这条挂起操作

    与 _pending_matches 的区别是多比一道 turn_id：同一个端连发两轮时，
    上一轮挂起的危险操作不能被这一轮的「确认」顺手放行（那一轮模型可能已经
    换了话题，用户说的「确认」指的并不是这件事）。
    """
    if not _pending_matches(p, sid, epoch, client, device):
        return False
    return (p.get("turn_id") or "") == (turn_id or "")


# ---------- WebSocket 连接管理 ----------

class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, msg: dict):
        # 并发发：这里串行 await 的话，一条卡住的连接（手机切到后台、网络半死
        # 但没断）就会把后面所有端的流式输出一起堵住
        targets = list(self.active)
        if not targets:
            return
        results = await asyncio.gather(*(ws.send_json(msg) for ws in targets),
                                       return_exceptions=True)
        for ws, res in zip(targets, results):
            if isinstance(res, BaseException):
                self.active.discard(ws)

    async def close_all(self, code: int = 1000):
        """按指定原因码断开所有连接（改令牌后用 4401，让前端回到鉴权页）"""
        for ws in list(self.active):
            try:
                await ws.close(code=code)
            except Exception:
                pass
        self.active.clear()


manager = ConnectionManager()


async def _broadcast_pad_order(action: str):
    """虚拟手柄建好/销毁时喊 QAM 插件去换 Steam 的控制器排序

    排序状态只在 Steam 进程内存里，只能在 Steam 页面上下文里调
    SteamClient.Input.SwapControllerOrder，所以真正干活的是插件前端；
    这里只要把消息广播出去（插件后端收到后再转给它自己的前端）。
    """
    n = len(manager.active)
    print(f"[DeckMeido] 广播手柄换序 {action} → {n} 个客户端")
    await manager.broadcast({"type": "pad_order", "action": action})


tools.set_pad_order_hook(_broadcast_pad_order)

# 设置页保存新令牌时的一次性通行证：允许旧令牌再完成这一次保存
_pending_token: str | None = None


# ---------- 认证 ----------

def _norm_token(value: str | None) -> str:
    """令牌统一按小写比较，输入不分大小写"""
    return (value or "").strip().lower()


def check_token(token: str | None = Query(default=None), x_token: str | None = Header(default=None)):
    if _norm_token(TOKEN) and _norm_token(TOKEN) in (_norm_token(token), _norm_token(x_token)):
        return
    raise HTTPException(status_code=401, detail="invalid token")


def check_token_any(token: str | None = Query(default=None), x_token: str | None = Header(default=None)):
    """改令牌专用：旧令牌或新令牌任一都能通过。

    设置页保存新令牌时，前端还带着旧令牌，而校验已经换成新的了，
    不放开这一条会导致「改完自己把自己锁在外面」。
    """
    ok = {_norm_token(TOKEN), _norm_token(_pending_token)} - {""}
    if _norm_token(token) not in ok and _norm_token(x_token) not in ok:
        raise HTTPException(status_code=401, detail="invalid token")


# ---------- 系统状态 ----------

def read_first(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def get_status(gaming: bool | None = None) -> dict:
    battery = read_first("/sys/class/power_supply/BAT1/capacity")
    charging = read_first("/sys/class/power_supply/BAT1/status")
    load = ""
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
        load = " ".join(load)
    except OSError:
        pass
    # 模式判据：gamescope 在 PipeWire 里有没有视频节点。以前用
    # `pgrep -x gamescope`，但 SteamOS 上进程名带着一长串参数，comm 被截到
    # 15 字节后反而匹配不上，pgrep 恒返回 1 —— 于是进了游戏模式还一直显示
    # 桌面、「画面」也不可用。PipeWire 节点只在游戏模式存在，而且抓帧用的
    # 就是这个判断，两处不会再打架。它不便宜，所以带缓存（见 tools.mode_is_gaming）。
    # 探针会起子进程，接口层用 to_thread 算好再传进来，别卡事件循环
    if gaming is None:
        gaming = tools.mode_is_gaming()
    win = context_window()
    used = _ctx_used or _estimate_tokens(
        _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
    pct = round(min(used / win, 1.0) * 100, 1) if win else 0
    return {
        "battery": int(battery) if battery.isdigit() else None,
        "charging": charging or None,
        "load": load or None,
        "mode": "gaming" if gaming else "desktop",
        # 后端是否正在跑一轮对话：前端重连后据此恢复 ⏹ 按钮状态，
        # 避免断联时正在跑的那轮看起来像「已经停了」
        "running": bool(current_task and not current_task.done()),
        # 本轮开始的时间戳（没有在跑则为 0）：前端据此显示「已等 N 秒」，
        # 工具链跑得久时也能看出后端还活着，而不是一屏静默的「没反应」
        "running_since": (store.turn.get("started") or 0) if (current_task and not current_task.done()) else 0,
        "llm_configured": llm.configured,
        "model": llm.model,
        "thinking": bool(llm.reasoning_effort),
        "context": {
            "messages": len(store.history),
            "chars": sum(len(str(m.get("content", ""))) for m in store.history),
            "used": used,
            "window": win,
            "percent": pct,
            "compact_at": round(COMPACT_RATIO * 100),
            # 最近一次请求的前缀缓存命中率（DeepSeek 才有；别家是 0，
            # 前端据此决定要不要显示「缓存 xx%」）
            "cache": llm.cache_stats(),
        },
        "workspace": str(tools.WORKSPACE),
        # 控制类控件（点击输入/窗口聚焦/原生打字/构建依赖）的实测结果，
        # 前端在电量徽章左侧显示成「x/x + 绿灯/黄灯/红灯」
        "controls": depcheck.summary(),
        # 桌面模式下执行命令/亮遮罩时是否正压着「别息屏」（前端不展示，排障用）
        "keep_awake": power.keeper.status(),
    }


# ---------- API ----------

app = FastAPI(title="DeckMeido")


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """页面本身禁止缓存。

    index.html 是全局唯一的前端入口，改完必须立刻生效。默认 StaticFiles 只带
    ETag + Last-Modified，而 QtWebEngine（原生客户端）和部分浏览器会拿旧副本，
    导致改完代码后客户端里还是老界面，看着像"改动没生效"。
    这里只对 HTML 文档关缓存，静态资源（图片/CSS/JS）照常走 ETag，不影响性能。
    """
    resp = await call_next(request)
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype:
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


class ChatRequest(BaseModel):
    message: str
    # 可选的附带图片（data URL 或纯 base64），供 Decky 插件把游戏画面直接塞进这一轮
    image: str | None = None
    image_prompt: str = "（这是当前游戏画面，结合画面回答用户的问题）"
    # 发起端身份：危险操作挂起后，只有「同一个端」说的「确认」才算数。
    # 不传的话挂起记录里是空值，插件端和网页端都会退化成同一个来源
    client: str | None = None
    device: str | None = None


_CTX_RE = re.compile(r"@\{([^}]+)\}")


def _pack_image(data: str, prompt: str = "") -> tuple[dict | None, bytes | None]:
    """解析并压缩一张图，返回 (多模态消息, 压缩后的字节)

    字节那份是给 _save_incoming_shot 落盘用的：各处显示和模型看到的必须是同一张，
    否则会出现「界面上一张、模型看到的另一张」。
    data URL 形如 data:image/png;base64,xxxx；裸 base64 按文件头认格式。
    MIME 是唯一的判断依据：宁可返回 None 让调用方报错，也不能把一段
    解析失败的字符串当成图片发给模型，那会变成一条纯粹的空提问。
    """
    import base64
    raw = (data or "").strip()
    if not raw:
        return None, None
    mime = "image/png"
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        meta = head[5:]
        mime = (meta.split(";")[0] or mime).lower()
        if not meta.lower().endswith(";base64"):
            raw = base64.b64encode(raw.encode()).decode()
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        return None, None
    try:
        blob = base64.b64decode(raw, validate=True)
    except Exception:
        return None, None
    # 裸 base64 没法从字符串本身看出格式，只能认文件头。插件现在发的是 JPEG
    # （PNG 有 12MB，超过 LLM 网关的请求体上限），按 PNG 标上去会让模型收错格式
    if not raw.startswith("data:"):
        if blob[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            mime = "image/webp"
    # 超过软门槛就压成 JPEG：插件那张 q90 的 1.8MB 截图 base64 有 240 万字符，
    # 越过 _MAX_IMG_CHARS 之后，重发历史时会被替换成「图片太大已略过」的占位符，
    # 模型从来没看到过这张图 —— 这就是「插件截的图传不上来」的真正原因
    if len(blob) > _IMG_SOFT_BYTES:
        packed = _jpeg_for_llm(blob)
        if packed is not blob:
            blob, mime = packed, "image/jpeg"
            raw = base64.b64encode(blob).decode()
    part = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{raw}"}},
        {"type": "text", "text": prompt or _shot_caption(1)},
    ]}
    return part, blob


def _jpeg_for_llm(blob: bytes, max_side: int = 1024, quality: int = 75) -> bytes:
    """把一张图压成适合发给模型的 JPEG

    插件截的是 1280x800 全屏 JPEG q90，一张游戏画面就有 1.8MB，base64 之后
    240 万字符，越过 _MAX_IMG_CHARS 就会被 _shrink_oversized_images 换成
    「图片太大已略过」—— 模型只看到这句话，于是让用户重发，用户看到的就是
    「插件截的图传不上来」。而 PC 网页上传的小图没这个问题，所以只有插件中招。
    压到 1024 宽 q75 只有 160KB，看图问答看不出差别，重发历史也安全。
    压不动就原样返回，交给原来的保护逻辑兜底。
    """
    try:
        from io import BytesIO

        from PIL import Image
        img = Image.open(BytesIO(blob))
        img.load()
        img.thumbnail((max_side, max_side))
        buf = BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        # 压完反而更大（本来就小的图）就保留原样
        return out if len(out) < len(blob) else blob
    except Exception:
        return blob


def _save_incoming_shot(blob: bytes) -> dict | None:
    """把随提问附带的画面落成文件，返回一条截图事件

    和模型自己截图（tools.screenshot）用同一个目录、同一个 /shots/ 前缀，
    前端不需要为两种来源写两套渲染。存不下来只影响「看不看得到图」，
    不影响这一轮问答，所以失败就返回 None。
    blob 传的是 _pack_image 压过的那份，界面显示的和模型看到的才是同一张。
    """
    ext = ".png"
    if blob[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        ext = ".webp"
    shots_dir = Path(__file__).resolve().parent / "static" / "shots"
    try:
        # parents=True：static/shots 在干净部署里并不存在，只写 exist_ok
        # 时 mkdir 会直接抛 FileNotFoundError，被下面 except 吞掉，
        # 表现就是「插件带的图永远上不了网页」而日志里一个字都没有
        shots_dir.mkdir(parents=True, exist_ok=True)
        # 清理门槛远大于「留最近 40 张」：模型那边现在也按文件名回读这张图
        # （见 _shot_ref_content），文件一删，历史里那条提问就变成了
        # 「界面破图 + 模型看不到画面」。900 张之后才会动到最老的那批，
        # 早就被上下文压缩带过去了
        old = sorted(shots_dir.glob("ask_*.*"), key=lambda p: p.stat().st_mtime)
        for p in old[:-900]:
            p.unlink(missing_ok=True)
        # 微秒级时间戳：连着两次截图落在同一毫秒会让后一张覆盖前一张
        name = f"ask_{int(time.time() * 1_000_000)}{ext}"
        (shots_dir / name).write_bytes(blob)
    except Exception as e:
        # 以前这里是完全静默的，图片传不上来时无从下手排查；
        # 失败只影响「看不看得到图」，这一轮问答照常继续
        print(f"[DeckMeido] 附带画面落盘失败（{shots_dir}）：{e!r}",
              file=sys.stderr)
        return None
    print(f"[DeckMeido] 附带画面已落盘：{name}（{len(blob)} 字节）",
          file=sys.stderr)
    return {"type": "screenshot", "name": "screenshot",
            "summary": "截屏询问时附带的画面", "image": f"/shots/{name}",
            # 原始文件名：历史里用它拼成 [SHOT:xxx] 标记，发给模型时再按文件读
            "file": name}


def _expand_context(text: str, workspace: Path | None = None) -> str:
    """把 @{路径} 引用展开为文件内容（每个最多 6000 字符）

    workspace 只在解析相对路径时用：Decky 插件以 root 身份跑，Path.home()
    是 /root，不显式指定的话插件发来的 @{文件} 会全部读不到。
    """
    root = workspace or tools.WORKSPACE

    def repl(m):
        p = Path(m.group(1)).expanduser()
        if not p.is_absolute():
            p = root / p
        try:
            # 和文件工具同一套边界：非 full 模式下不许把工作区外的文件
            # （配置里的 Key、会话记录）读进提示词发给上游
            p = tools.resolve_in_workspace(str(p))
            body = p.read_text(encoding="utf-8", errors="replace")[:6000]
            return f"\n[文件 {p} 的内容]：\n```\n{body}\n```\n"
        except tools.OutsideWorkspace:
            return (f"\n[文件 {p} 不在工作区内，已跳过。"
                    f"需要读它请让用户切到「完全访问」]\n")
        except OSError as e:
            return f"\n[文件 {p} 读取失败：{e}]\n"
    return _CTX_RE.sub(repl, text) if "@{" in text else text


_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".sh",
             ".yml", ".yaml", ".toml", ".ini", ".cfg", ".xml", ".html", ".css",
             ".c", ".h", ".cpp", ".rs", ".go", ".java", ".sql", ".conf"}
# 单个附件上限，防止一次拖进几个 G 的文件把服务拖死
_UPLOAD_MAX = 20 * 1024 * 1024


async def _extract_text(raw: bytes, ext: str):
    """尽量把文档抽成纯文本；抽不出来返回 None（按二进制附件处理）"""
    if ext in _TEXT_EXT:
        return raw.decode("utf-8", errors="replace")
    if ext == ".docx":
        # docx 就是个 zip，正文在 word/document.xml，去掉标签即可
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        except (KeyError, zipfile.BadZipFile, OSError):
            return None
        xml = xml.replace("</w:p>", "\n")
        return re.sub(r"<[^>]+>", "", xml).strip()
    if ext == ".pdf":
        # 有 pdftotext 就抽文字层，没有就算了。
        # 走 to_thread：大 PDF 解析可能要十几秒，阻塞事件循环会让所有 ws 心跳停摆
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["pdftotext", "-q", "-", "-"], input=raw,
                capture_output=True, timeout=25,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace") or None
    return None


@app.post("/api/upload", dependencies=[Depends(check_token)])
async def api_upload(file: UploadFile):
    """上传附件。

    图片 → static/uploads/，返回 /uploads/xxx.png，前端插成 ![图片](url) 走多模态；
    其他文件 → 工作区 uploads/，返回相对工作区的路径，前端插成 @{uploads/xxx}，
    由 _expand_context 带上正文（文本类直接读，docx/pdf 现抽文字）。
    """
    try:
        raw = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取上传内容失败：{e}") from e
    if len(raw) > _UPLOAD_MAX:
        raise HTTPException(status_code=413, detail="文件超过 20MB")
    fn = Path(file.filename or "file")
    ext = fn.suffix.lower()
    stem = re.sub(r"[^\w\-.]+", "_", fn.stem)[:40] or "file"
    ts = int(time.time())

    if ext in _IMG_EXT or not ext:
        ext = ext if ext in _IMG_EXT else ".png"
        up = STATIC_DIR / "uploads"
        up.mkdir(exist_ok=True)
        name = f"up_{ts}{ext}"
        (up / name).write_bytes(raw)
        return {"url": f"/uploads/{name}", "kind": "image", "name": fn.name}

    up = tools.WORKSPACE / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    body = await _extract_text(raw, ext)
    if body is not None:
        # 有文字层就存成 .txt，@{} 展开时不会喂二进制给模型
        keep = up / f"{ts}_{stem}.txt"
        keep.write_text(f"# {fn.name}\n\n{body}", encoding="utf-8")
        kind = "text"
    else:
        keep = up / f"{ts}_{stem}{ext}"
        keep.write_bytes(raw)
        kind = "binary"
    rel = str(keep.relative_to(tools.WORKSPACE))
    return {"url": f"/uploads/{keep.name}", "path": rel, "kind": kind, "name": fn.name}


@app.get("/api/files", dependencies=[Depends(check_token)])
async def api_files():
    """列出工作区文件（@ 引用候选）"""
    out = []
    try:
        for p in sorted(tools.WORKSPACE.rglob("*")):
            if p.is_file() and not any(part.startswith(".") for part in p.parts):
                out.append(str(p.relative_to(tools.WORKSPACE)))
                if len(out) >= 300:
                    break
    except OSError:
        pass
    return {"workspace": str(tools.WORKSPACE), "files": out}


# 文件浏览器预览用的分类。只影响面板里显示什么图标、用哪种方式预览，
# 和「能不能读」无关，所以放成白名单也无所谓
_KIND_MAP = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"},
    "video": {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv"},
    "audio": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".oga"},
    "pdf": {".pdf"},
    "text": {".txt", ".md", ".log", ".csv", ".json", ".toml", ".ini", ".cfg", ".yml",
             ".yaml", ".xml", ".html", ".css", ".js", ".ts", ".py", ".sh", ".c", ".h",
             ".cpp", ".rs", ".go", ".java", ".sql", ".conf", ".srt", ".desktop"},
}


def _file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    for kind, exts in _KIND_MAP.items():
        if ext in exts:
            return kind
    return "other"


@app.get("/api/preview", dependencies=[Depends(check_token)])
async def api_preview(path: str, raw: int = 0):
    """给文件浏览器预览用：吐任意路径的文件

    和 /files/{path} 不一样：那个锁死在工作区里，只给对话附件用；
    面板要能预览 AI 正在看的任何目录，所以这里认绝对路径。
    默认回 JSON（元信息 + 文本前 64KB，前端一次拿齐）；
    raw=1 时直接回文件本体，给 <img>/<video>/<audio>/<iframe> 用——
    视频/音频要能拖进度条就得支持 Range，所以走 FileResponse。
    """
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if raw:
        # FileResponse 自带 Range / ETag 处理，浏览器可以边下边播
        return FileResponse(target)
    kind = _file_kind(target.name)
    try:
        stat = target.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    info = {"path": str(target), "name": target.name, "kind": kind,
            "size": stat.st_size, "mtime": stat.st_mtime}
    if kind == "text":
        try:
            info["text"] = target.read_bytes()[:65536].decode("utf-8", "replace")
        except OSError as e:
            info["error"] = f"读取失败：{e}"
    return info


@app.get("/api/browse", dependencies=[Depends(check_token)])
async def api_browse(path: str = ""):
    """列单层目录内容（右侧文件浏览器用）

    与 /api/files 的区别：那个只吐工作区里的文件、一次全量；这里要能浏览
    任意目录（AI 可能在看 /home/deck/xxx），所以只列一层、按需展开。
    路径不存在或没权限时返回 error，前端显示成一行提示而不是整块报错。
    """
    raw = (path or "").strip()
    # 不传 path 就回到工作区，用户第一次打开面板看到的是自己的工作区
    target = Path(raw).expanduser() if raw else tools.WORKSPACE
    if not target.is_absolute():
        # 相对路径一律相对工作区解释，避免 cwd 变化导致行为漂移
        target = tools.WORKSPACE / target
    # AI 给的是文件路径时列它所在的目录并把它标出来，面板据此高亮；
    # 联动事件里不带 is_dir 也能靠这个兜住
    select = ""
    try:
        target = target.resolve()
        if target.is_file():
            select = target.name
            target = target.parent
    except OSError:
        return {"path": raw, "parent": None, "entries": [], "error": "路径无效"}
    if not target.is_dir():
        return {"path": str(target), "parent": None, "entries": [],
                "error": "目录不存在或不是文件夹"}
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                item = {"name": e.name, "dir": is_dir}
                if not is_dir:
                    # kind 给前端挑图标和预览方式用（image/video/audio/pdf/text/other）
                    item["kind"] = _file_kind(e.name)
                    try:
                        item["size"] = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                entries.append(item)
    except PermissionError:
        return {"path": str(target), "parent": None, "entries": [],
                "error": "没有权限读取这个目录"}
    except OSError as e:
        return {"path": str(target), "parent": None, "entries": [],
                "error": f"读取失败：{e}"}
    # 文件夹在前、同类按名字排，隐藏文件照样列出（用户可能就是要看 .config）
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = str(target.parent) if str(target.parent) != str(target) else None
    return {"path": str(target), "parent": parent, "entries": entries, "select": select}


@app.get("/api/terminal", dependencies=[Depends(check_token)])
async def api_terminal():
    """当前正在跑（或刚跑完）的那条命令，给右侧终端面板轮询

    只返回最近一条：历史在面板本地攒。没有命令时 state 为 null。
    """
    return {"state": tools.CURRENT_CMD}


@app.post("/api/terminal/stop", dependencies=[Depends(check_token)])
async def api_terminal_stop():
    """终止面板里正在跑的那条命令（小窗上的终止按钮）

    先砍进程树（按登记表），再取消正在等它的对话轮次。不 cancel 的话，
    /api/terminal/run 那个请求会一直挂在里面等子进程，前端按钮永远转圈；
    但它可能是用户自己敲的，没有对话轮次可取消，所以这一步允许为空。
    """
    killed = await tools.abort_all_procs()
    if current_task and not current_task.done():
        current_task.cancel()
        set_overlay(False)
    return {"ok": True, "killed": killed}


# 面板自己敲的命令：和白名单无关，但同样要过危险判定，
# 用户手动敲的也按当前权限模式审批，避免网页端变成绕过审批的后门
_term_lock: asyncio.Lock | None = None


def _term_lock_get() -> asyncio.Lock:
    """惰性建锁：模块导入时还没有事件循环，提前 new 出来会绑到错的循环上"""
    global _term_lock
    if _term_lock is None:
        _term_lock = asyncio.Lock()
    return _term_lock


@app.post("/api/terminal/input", dependencies=[Depends(check_token)])
async def api_terminal_input(payload: dict):
    """把面板里敲的按键交给正在跑的那条 tty 命令（ncdu / top / vim 的键盘）

    前端只负责把按键翻成终端转义序列（方向键是 ESC [ A 这种），这里原样写进
    子进程的 stdin，不走审批：这是用户已经批准过的那条命令的运行时输入。
    """
    data = str(payload.get("data") or "")
    if not data:
        return {"ok": False, "written": False}
    ok = await tools.feed_tty_input(data)
    return {"ok": True, "written": ok}


@app.post("/api/terminal/run", dependencies=[Depends(check_token)])
async def api_terminal_run(payload: dict):
    """面板里手动执行一条命令（终端 tab 的输入框）"""
    cmd = str(payload.get("command") or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="命令为空")
    # 和对话里同一个审批通道：手动模式一律先问，智能模式只拦危险命令，
    # 完全访问直接跑。面板不能变成绕过审批的后门，但也不该自己发明一套规则
    needs_approval = (PERMISSION_MODE == "manual"
                      or (PERMISSION_MODE == "auto" and tools.is_dangerous(cmd)))
    if needs_approval:
        # 挂进和对话同一份 pending_action，之后由对话里那句「确认」放行
        global pending_action
        args = {"command": cmd, "reason": "终端面板手动执行"}
        pending_action = {"name": "run_command", "args": args,
                          "description": tools.describe("run_command", args),
                          "sid": store.current_sid, "epoch": store.epoch,
                          "turn_id": store.turn.get("turn_id") or "",
                          "client": store.turn.get("client") or "",
                          "device": store.turn.get("device") or ""}
        return {"ok": False, "pending": True,
                "description": pending_action["description"]}
    # 串行执行：面板和后端共用一份 CURRENT_CMD，两条命令并行会互相覆盖状态
    lock = _term_lock_get()
    if lock.locked():
        raise HTTPException(status_code=409, detail="上一条命令还在执行")
    async with lock:
        try:
            # tty=True：面板里敲的命令起伪终端。不开的话 stdin 是 /dev/null，
            # 编辑器、ssh、带确认的安装器都会报「stdin is not a TTY」直接退出
            out = await tools.execute("run_command",
                                      {"command": cmd, "reason": "面板手动执行", "tty": True})
        except Exception as e:
            return {"ok": False, "output": f"执行失败：{e}"}
    # 面板靠轮询 /api/terminal 收尾，这里顺带把最终状态带回去，省一次往返
    return {"ok": True, "output": out, "state": tools.CURRENT_CMD}


@app.get("/files/{path:path}", dependencies=[Depends(check_token)])
async def api_workspace_file(path: str):
    """下载工作区里的文件（网页端附件卡片用）

    路径必须落在工作区内：模型给的链接是相对工作区的路径，这里只认这个根，
    防止 ../ 一路穿到工作区外面去。
    """
    root = tools.WORKSPACE.resolve()
    try:
        target = (root / path).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if root != target and root not in target.parents:
        raise HTTPException(status_code=403, detail="outside workspace")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


def _workspace_file(rel: str) -> Path:
    """把「相对工作区的路径」解析成真实文件，越界或不存在一律 404

    模型给的链接是相对工作区的路径，这里只认这个根，防止 ../ 一路穿到工作区外面去。
    """
    root = tools.WORKSPACE.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if root != target and root not in target.parents:
        raise HTTPException(status_code=403, detail="outside workspace")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return target


@app.post("/api/file_stat_batch", dependencies=[Depends(check_token)])
async def api_file_stat_batch(payload: dict):
    """附件卡片补大小（批量）：{paths: [...]} → {sizes: {path: 字节数}}

    一条消息里可能有十几张卡片，逐张发请求在手机上就是十几次往返，
    这里一次问完；查不到的路径直接不出现在结果里，前端就不显示大小。
    """
    paths = payload.get("paths") or []
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="paths must be a list")
    sizes: dict[str, int] = {}
    for rel in paths[:200]:          # 条数封顶：这是一次 UI 补全，不是文件浏览器
        try:
            sizes[str(rel)] = _workspace_file(str(rel)).stat().st_size
        except (HTTPException, OSError):
            continue
    return {"sizes": sizes}


@app.get("/api/zip", dependencies=[Depends(check_token)])
async def api_zip(path: list[str] = Query(default=[]), name: str = Query(default="files.zip")):
    """把多个工作区文件打成一个 zip 下载（网页端「打包下载」用）

    在内存里打：一次打包的通常是模型刚生成的几个文档，几十 MB 量级，
    落到临时文件反而要操心清理。
    """
    if not path:
        raise HTTPException(status_code=400, detail="no files")
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in path[:200]:
            try:
                target = _workspace_file(rel)
            except HTTPException:
                continue
            # 同名文件加序号，别让后面的覆盖前面的
            arc = target.name
            n = 1
            while arc in used:
                n += 1
                arc = f"{target.stem}_{n}{target.suffix}"
            used.add(arc)
            try:
                zf.write(target, arcname=arc)
            except OSError:
                continue
    if not used:
        raise HTTPException(status_code=404, detail="file not found")
    data = buf.getvalue()
    safe = re.sub(r"[^\w\-.]+", "_", Path(name).name)[:60] or "files.zip"
    if not safe.lower().endswith(".zip"):
        safe += ".zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.post("/api/file_stat", dependencies=[Depends(check_token)])
async def api_file_stat(payload: dict):
    """附件卡片补大小：{path: 相对工作区的路径} → {size, name}"""
    rel = str(payload.get("path", "")).strip()
    if not rel:
        raise HTTPException(status_code=404, detail="file not found")
    target = _workspace_file(rel)
    try:
        return {"size": target.stat().st_size, "name": target.name}
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")


@app.get("/api/status", dependencies=[Depends(check_token)])
async def api_status():
    # 模式探针要起 pw-dump，放线程里跑，别阻塞事件循环（WS 也在同一个 loop 上）
    return get_status(await asyncio.to_thread(tools.mode_is_gaming))


@app.get("/api/feed", dependencies=[Depends(check_token)])
async def api_feed(after: int = Query(default=0)):
    """增量拉取新消息：返回 id > after 的条目和最新 seq"""
    return {"items": [m for m in feed if m["id"] > after], "seq": _feed_seq}


@app.post("/api/chat", dependencies=[Depends(check_token)])
async def api_chat(req: ChatRequest, workspace: str | None = Query(default=None)):
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    # workspace：Decky 插件以 root 身份跑，Path.home() 是 /root，必须由它把
    # 真实工作区（/home/deck/...）传进来，否则 @{文件} 引用全部读不到
    root = Path(workspace).expanduser() if workspace else None
    if root is not None and not root.is_dir():
        root = None
    return await start_chat(text, image=req.image, image_prompt=req.image_prompt,
                            workspace=root, client=req.client, device=req.device)


# 当前正在处理的对话任务（用于终止按钮）
current_task: asyncio.Task | None = None


async def start_chat(text: str, announce: bool = True, **kwargs):
    """把一轮对话放到可取消的任务里执行。announce=False 时不重复广播用户消息（重试用）

    **kwargs 原样转给 handle_chat（图片、工作区根），插件那条路要用。
    """
    global current_task
    current_task = asyncio.create_task(handle_chat(text, announce, **kwargs))
    try:
        return await current_task
    except asyncio.CancelledError:
        return {"reply": "已终止当前操作", "events": [], "cancelled": True}
    finally:
        current_task = None


@app.post("/api/cancel", dependencies=[Depends(check_token)])
async def api_cancel():
    if current_task and not current_task.done():
        current_task.cancel()
        # 遮罩进程在遮罩消失前会重新拉一次状态，这里立刻翻成 false，
        # 用户点完遮罩就不会出现「已终止」又闪回操作中的画面
        set_overlay(False)
        # 第二道保险：取消链要是有哪一处 await 卡住了（孙进程占着 stdout 管道就
        # 会这样），光 cancel() 停不下来，界面会一直卡在「正在执行」。这里直接把
        # 登记在案的子进程树砍掉，让那一轮无论如何都能收尾。
        killed = await tools.abort_all_procs()
        return {"ok": True, "killed": killed}
    # 任务已结束但可能还留着后台子进程，一并清掉，避免下次点终止没反应
    killed = await tools.abort_all_procs()
    return {"ok": False, "detail": "no active task", "killed": killed}


class ConfigUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    # 显式要求清空 Key；不传则空字符串一律按「不改」处理
    clear_api_key: bool = False
    model: str | None = None
    tts_enable: bool | None = None
    permission_mode: str | None = None
    # 深度思考档位：空串 / low / medium / high（前端滑条的「无 / 低 / 中 / 高」）
    reasoning_effort: str | None = None
    workspace: str | None = None
    token: str | None = None
    # 人设：整体写回 meidosoul.json，空字符串表示「恢复默认」
    persona_name: str | None = None
    # 身份定位：拼在 system prompt 第一句「你是 X，<traits>」，决定她是谁
    persona_traits: str | None = None
    # 补充设定：整段追加到 system prompt 末尾，比上面的表单自由
    persona_extra: str | None = None
    persona_address: str | None = None
    persona_tone: str | None = None
    persona_ending: str | None = None
    persona_suffix: str | None = None
    persona_forbid: str | None = None
    # 「截屏询问」提示词模板：空字符串表示恢复默认
    ask_template: str | None = None
    # 更新源：mirror / github / mirror-only / github-only
    update_source: str | None = None


class AskTemplateUpdate(BaseModel):
    # 空 / 全空白表示恢复默认模板
    text: str = ""


class MemoryItem(BaseModel):
    """设置页手工教学用的记忆条目（text 为空 = 删除这条）"""
    id: str | None = None
    kind: str = "note"
    text: str = ""


class SoulImport(BaseModel):
    """导入备份：人设 + 记忆。两部分都可缺省，缺的保持原样"""
    persona: dict | None = None
    memory: list[dict] | None = None
    replace: bool = True


def _mask_key(key: str) -> str:
    """把 Key 打码成 sk-7sw…56D5 这种样子：够认出是哪把，又不可用。

    为什么不直接把明文发给前端：只要拿到访问令牌（默认还是 meido），
    任何一台设备打开设置页就能把 Key 抄走。打码后前端只用于「展示已配置」，
    真正用 Key 的请求都在服务端完成。
    """
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


@app.get("/api/config", dependencies=[Depends(check_token)])
async def api_get_config():
    return {
        "base_url": llm.base_url,
        # 明文 Key 不再下发，只给打码值和「有没有配」的布尔量
        "api_key": _mask_key(llm.api_key),
        "api_key_set": bool(llm.api_key.strip()),
        "api_key_masked": _mask_key(llm.api_key),
        "model": llm.model,
        "reasoning_effort": llm.reasoning_effort,
        "tts_enable": tts.enable,
        "permission_mode": PERMISSION_MODE,
        "workspace": str(tools.WORKSPACE),
        "token": TOKEN,
        "port": CFG["server"].get("port", 18765),
        "llm_configured": llm.configured,
        "version": VERSION,
        # 人设：设置页表单用，默认值和后台提示词里的一致
        "persona_name": PERSONA.get("name", "DeckMeido"),
        "persona_traits": PERSONA.get("traits", ""),
        "persona_extra": PERSONA.get("extra", ""),
        "persona_address": (PERSONA.get("style") or {}).get("address", "主人"),
        "persona_tone": (PERSONA.get("style") or {}).get("tone", ""),
        "persona_ending": (PERSONA.get("style") or {}).get("ending", ""),
        "persona_suffix": (PERSONA.get("style") or {}).get("suffix", ""),
        # 禁用词列表在界面上是一行、用逗号隔开
        "persona_forbid": "，".join((PERSONA.get("style") or {}).get("forbid") or []),
        # 「截屏询问」的提示词模板：插件设置区里可以直接改
        "ask_template": ASK_TEMPLATE,
        "ask_template_default": DEFAULT_ASK_TEMPLATE,
        # 更新源：updater 从 config.toml 的 [update] 段读，缺省即默认值
        "update_source": updater.update_cfg()["source"],
        "update_source_label": updater.source_label(),
        "update_mirror": updater.update_cfg()["mirror"],
        # 长期记忆的条数概览（正文单独走 /api/memory，设置页按需拉）
        "memory": memory.stats(),
    }


@app.get("/api/memory", dependencies=[Depends(check_token)])
async def api_get_memory():
    """全部长期记忆，按「类别 + 创建时间」排好，和注入顺序一致"""
    items = memory.load()["items"]
    order = list(memory.KINDS)
    items = sorted(items, key=lambda x: (order.index(x["kind"]), x["created"]))
    return {"items": items, "kinds": [{"id": k, "name": v} for k, v in memory.KINDS.items()]}


@app.post("/api/memory", dependencies=[Depends(check_token)])
async def api_save_memory(req: MemoryItem):
    """新增 / 修改 / 删除一条记忆

    带 id 且 text 为空 = 删除；带 id 且 text 非空 = 修改；不带 id = 新增。
    """
    if not req.id:
        item = memory.add(req.kind, req.text)
        if not item:
            raise HTTPException(status_code=400, detail="内容不能为空")
        return {"ok": True, "item": item}
    if not memory.update(req.id, req.text, req.kind):
        raise HTTPException(status_code=404, detail="这条记忆已不存在")
    return {"ok": True}


@app.delete("/api/memory/{mid}", dependencies=[Depends(check_token)])
async def api_delete_memory(mid: str):
    if not memory.remove(mid):
        raise HTTPException(status_code=404, detail="这条记忆已不存在")
    return {"ok": True}


@app.delete("/api/memory", dependencies=[Depends(check_token)])
async def api_clear_memory():
    memory.clear()
    return {"ok": True}


# ---------- 人设 + 记忆的导出 / 导入 ----------
# 这两样就是「她这个人」的全部：人设在 meidosoul.json，记忆在 memory.json，
# 换机器、重装系统、想备份一份，都应该一次拷走。

@app.get("/api/soul/export", dependencies=[Depends(check_token)])
async def api_export_soul():
    """导出人设与全部记忆，返回一个可以直接存盘的 JSON"""
    items = memory.load()["items"]
    order = list(memory.KINDS)
    items = sorted(items, key=lambda x: (order.index(x["kind"]), x["created"]))
    return {
        "kind": "meidosoul",
        "version": 1,
        "exported": time.time(),
        "persona": persona.load(),
        "memory": items,
    }


@app.post("/api/soul/import", dependencies=[Depends(check_token)])
async def api_import_soul(req: SoulImport):
    """导入备份。replace=True 覆盖，False 则只补缺（人设仍整体覆盖）"""
    if req.persona is not None:
        persona.save(req.persona)
        reload_persona()
    added = 0
    if req.memory is not None:
        if req.replace:
            memory.clear()
        # 逐条走 memory.add：它自己会去重、截断、按上限淘汰，
        # 备份文件里就算有脏数据也不会把记忆撑坏
        for it in req.memory:
            if isinstance(it, dict) and memory.add(str(it.get("kind") or "note"),
                                                   str(it.get("text") or "")):
                added += 1
    return {"ok": True, "added": added, "memory": memory.stats()}


@app.get("/api/token/gen", dependencies=[Depends(check_token)])
async def api_token_gen():
    """给设置页的「随机生成」按钮用，只返回不保存"""
    return {"token": gen_token()}


def _norm_base_url(url: str) -> str:
    """接口地址去掉末尾斜杠，空串原样返回（= 清空）

    地址原样保存，不擅自补 /v1：DeepSeek 官方两种写法都认，OpenAI 必须带 /v1，
    本地推理服务多半不能带 —— 统一改写只会在界面上留下和用户填的不一样的值。
    """
    return (url or "").strip().rstrip("/")


async def _pick_model(base_url: str, api_key: str) -> str:
    """换了服务商后自动挑一个模型：原模型还在就用它，否则取列表里第一个

    这个函数只负责「猜个更合适的模型名」，猜不到就不该拦着用户保存配置。
    旧版会把探活异常（Key 写错、服务商抽风、断网 → 401/超时）直接冒到
    外层，整个 POST /api/config 变成 500：新填的 Key 存不进去，
    用户看到「保存失败」但输入框又被清空，等于把配置搞坏了。
    现在一律吞掉异常，退回当前模型名，由「保存后拉模型列表」那步另行提示。
    """
    fallback = llm.model
    try:
        probe = LLMClient(base_url=_norm_base_url(base_url), api_key=api_key,
                          model=fallback, timeout=30.0)
        try:
            names = await probe.list_models()
        finally:
            await probe.close()
    except Exception:
        return fallback
    if fallback in names:
        return fallback
    if not names:
        return fallback
    return names[0]


@app.post("/api/config", dependencies=[Depends(check_token_any)])
async def api_set_config(req: ConfigUpdate):
    global _pending_token
    llm_updates = {}
    if req.base_url is not None:
        llm_updates["base_url"] = _norm_base_url(req.base_url)
    # Key 的三条「不覆盖」规则，都是为了避免把已配置的 Key 冲掉：
    #   1) 字段没传（None）           → 本次不改 Key
    #   2) 传了空字符串               → 前端没拿到值，按「不改」处理
    #   3) 传的是打码串（含 …）        → 前端回传了展示用的脱敏值，不能当真
    # 想真清空，用 clear_api_key=true 显式声明。
    # 旧逻辑是「传了空就写成空」，于是在新设备上打开设置页、只是改了个令牌
    # 或权限，就会把 Key 一并抹成空串 —— 对话当场失效，且无处可恢复。
    if req.clear_api_key:
        llm_updates["api_key"] = ""
    elif req.api_key is not None:
        incoming = req.api_key.strip()
        if incoming and "…" not in incoming and "..." not in incoming:
            llm_updates["api_key"] = incoming
    if req.model is not None:
        llm_updates["model"] = req.model.strip()
    elif llm_updates.get("api_key") and llm_updates["api_key"] != llm.api_key:
        # 换了 Key 而没指定模型：保持原模型若它仍可用，否则挑列表里第一个正规模型名
        # 注意用 llm_updates 里的值：直接读 req.api_key 的话，打码串也会进这里，
        # 拿 "sk-7sw…56D5" 去问服务商只会 401
        llm_updates["model"] = await _pick_model(req.base_url or llm.base_url, llm_updates["api_key"])
    updates = {}
    if llm_updates:
        updates["llm"] = llm_updates
    if req.tts_enable is not None:
        updates["tts"] = {"enable": req.tts_enable}
    if req.reasoning_effort is not None and req.reasoning_effort in ("", "low", "medium", "high"):
        # 深度思考滑条：空串 = 不带参数（不思考），其余原样写进 [llm]。
        # 空串要显式落盘，不能当作「没传」忽略，否则关掉之后再打开还是旧的档位
        llm_updates["reasoning_effort"] = req.reasoning_effort
    if req.permission_mode in ("manual", "auto", "full"):
        updates["security"] = {"permission_mode": req.permission_mode}
    if req.workspace is not None and req.workspace.strip():
        updates["workspace"] = {"dir": req.workspace.strip()}
    if req.ask_template is not None:
        # 清空即恢复默认：模板是「截屏询问」唯一的提问来源，
        # 留成空串会让这个按钮发不出去任何内容
        tpl = req.ask_template.strip() or DEFAULT_ASK_TEMPLATE
        updates.setdefault("agent", {})["ask_template"] = tpl
    if req.persona_name is not None:
        # 人设存 meidosoul.json（和记忆放在一起，方便导出备份），不写 config.toml。
        # 留空就写回默认值，避免空串把人设搞没。
        forbid = [w.strip() for w in re.split(r"[,，、\s]+", req.persona_forbid or "") if w.strip()]
        persona.save({
            "name": req.persona_name.strip() or "DeckMeido",
            # traits / extra 没传（旧客户端）就保持原样，别把身份定位洗成默认
            "traits": PERSONA.get("traits", "") if req.persona_traits is None else req.persona_traits,
            "extra": PERSONA.get("extra", "") if req.persona_extra is None else req.persona_extra,
            "style": {
                "address": req.persona_address or "",
                "tone": req.persona_tone or "",
                "ending": req.persona_ending or "",
                "suffix": req.persona_suffix or "",
                "forbid": forbid,
            },
        })
        reload_persona()
    if req.update_source in ("mirror", "github", "mirror-only", "github-only"):
        # 只写 source；mirror / github_repo 的地址留给 [update] 段（一般不用改）
        updates["update"] = {"source": req.update_source}
    if req.token is not None:
        # 统一存成小写，避免 config 里大小写混着看花眼（校验本来就不分大小写）
        new_token = req.token.strip().lower()
        # 令牌只允许 4-32 位数字/字母，太短容易被猜，太长在掌机上不好敲
        if not re.fullmatch(r"[a-z0-9]{4,32}", new_token):
            raise HTTPException(status_code=400, detail="令牌需为 4-32 位数字或字母")
        updates["server"] = {"token": new_token}
    if updates:
        # 改令牌期间临时放行旧令牌，改完清掉；令牌换掉后旧连接一并踢下线
        _pending_token = updates.get("server", {}).get("token")
        try:
            old_token = TOKEN
            update_config(updates)
        finally:
            _pending_token = None
        if TOKEN != old_token:
            await manager.close_all(4401)
    return {"ok": True, "llm_configured": llm.configured, "workspace": str(tools.WORKSPACE),
            "token": TOKEN,
            # 回传归一之后的实值：前端滑条按它落位（比如 DeepSeek 上的「中」实际是 high）
            "reasoning_effort": llm.reasoning_effort,
            "model": llm.model}


@app.post("/api/tts", dependencies=[Depends(check_token_any)])
async def api_set_tts(enabled: bool = Query(...)):
    """只切语音播报开关：给 Decky 插件面板用

    QAM 里没法像网页设置页那样填一整张表单，只带一个参数，走的还是
    update_config 同一条热更新路径（tts.enable 立即生效，不用重启）。
    """
    update_config({"tts": {"enable": bool(enabled)}})
    return {"ok": True, "tts_enable": tts.enable}


@app.get("/api/ask-template", dependencies=[Depends(check_token)])
async def api_get_ask_template():
    """「截屏询问」提示词模板：插件面板展开设置区时读一次"""
    return {"ok": True, "text": ASK_TEMPLATE, "default": DEFAULT_ASK_TEMPLATE}


@app.post("/api/ask-template", dependencies=[Depends(check_token_any)])
async def api_set_ask_template(req: AskTemplateUpdate):
    """改「截屏询问」提示词模板：空 / 全空白表示恢复默认

    模板只影响下一次点「截屏询问」，已经发出的那一轮不受影响。
    """
    tpl = req.text.strip() or DEFAULT_ASK_TEMPLATE
    update_config({"agent": {"ask_template": tpl}})
    return {"ok": True, "text": ASK_TEMPLATE}


# ---------- 版本与升级 ----------

# 不校验令牌：鉴权页在登录前就要显示版本号
@app.get("/api/version")
async def api_version():
    return {
        "version": VERSION,
        "repo": updater.REPO,
        "update_source": updater.update_cfg()["source"],
        "update_source_label": updater.source_label(),
        "is_git": (INSTALL_DIR / ".git").exists(),
        "install_dir": str(INSTALL_DIR),
        "update": update_cache,
    }


@app.get("/api/update/check", dependencies=[Depends(check_token)])
async def api_update_check():
    try:
        info = await updater.check()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return info


@app.post("/api/update/apply", dependencies=[Depends(check_token)])
async def api_update_apply():
    """从网页一键升级：拉取代码后交给 install.sh upgrade 重启服务

    升级会把正在运行的这个服务换掉，所以交给独立会话的脚本进程去做，
    接口只负责把任务踢起来并立刻返回。
    """
    try:
        return await asyncio.to_thread(updater.apply, True)
    except RuntimeError as exc:
        msg = str(exc)
        raise HTTPException(status_code=400 if "不是 git" in msg else 502, detail=msg)


@app.post("/api/restart", dependencies=[Depends(check_token)])
async def api_restart():
    """重启核心（后端服务自身）

    这里**不能自己杀自己**。后端多半是被 Decky 插件用
    `runuser -u deck -- <venv>/bin/python -m uvicorn ...` 降权拉起来的，
    自己 `pkill -f 'uvicorn server.main:app'` 会把外层的 runuser 一起带走，
    再用 nohup 就地拉起的那份既没有 runuser 包装、又跟插件失去联系，
    结果就是旧进程没了、新进程没起来，重启变成「把后端关掉」。
    而且插件是唯一知道完整启动命令的一方。

    所以这里只表明「需要重启」，由调用方（插件）去执行。插件拿到的
    `delegate: true` 就是让它走自己的 runuser 命令重放一遍。
    直接 curl 这个接口不会真的重启，这也避免了误调用把服务弄挂。
    """
    return {"ok": True, "version": VERSION, "delegate": True}


# 后台自动检查升级：启动时查一次，之后每天一次；只提示，不自动安装
update_cache: dict = {}
UPDATE_INTERVAL = 24 * 3600


async def update_watch_loop():
    global update_cache
    while True:
        try:
            info = await updater.check()
            # used=实际问到结果的源；fallback=前面失败的源（用于提示「已切备用源」）
            used = info.get("source") or ""
            base = {"source": used, "source_label": info.get("source_label", ""),
                    "fallback": info.get("fallback_from") or []}
            if info["has_update"]:
                update_cache = {"has_update": True, "latest": info["tag"],
                                "current": info["current"], "url": info.get("url", ""), **base}
                await manager.broadcast({"type": "update_available", **update_cache})
            else:
                update_cache = {"has_update": False, "current": info["current"], **base}
        except Exception as exc:
            # 两个源都连不上（断网、被墙）属正常情况，记下原因下次再试
            update_cache = {"has_update": False, "current": updater.current_version(),
                            "error": str(exc)[:300]}
        await asyncio.sleep(UPDATE_INTERVAL)


@app.on_event("startup")
async def _start_update_watch():
    asyncio.create_task(update_watch_loop())


@app.get("/api/models", dependencies=[Depends(check_token)])
async def api_models(base_url: str = "", api_key: str = ""):
    """向服务商拉取可用模型列表，供前端下拉框自动填充

    带上 base_url/api_key 时用面板里刚填的值临时探测（不落盘），
    这样用户粘贴新地址和 Key 后不点保存也能先看到有哪些模型。
    """
    try:
        if base_url.strip():
            probe = LLMClient(
                base_url=_norm_base_url(base_url),
                api_key=(api_key or llm.api_key).strip(),
                model=llm.model,
                timeout=30.0,
            )
            try:
                names = await probe.list_models()
            finally:
                await probe.close()
        else:
            names = await llm.list_models()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"拉取模型列表失败：{exc}")
    return {"models": names}


@app.post("/api/retry", dependencies=[Depends(check_token)])
async def api_retry():
    global pending_action
    # 只有本会话挂起的操作才挡重试；别的会话挂着的与本端无关
    if pending_action and _pending_matches(
            pending_action, store.current_sid, store.epoch, None, None):
        raise HTTPException(status_code=400, detail="有待确认的操作，先回复确认或取消")
    if store.history and store.history[-1]["role"] == "assistant":
        store.history.pop()
    if not store.history or store.history[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="没有可重试的用户消息")
    text = store.history.pop()["content"]
    return await start_chat(text, announce=False)


def _safe_save(sid: str, hist: list[dict]) -> None:
    """收尾落盘：磁盘满/权限出错时不能让「复位 turn.active」也跟着跳过"""
    try:
        save_session_snapshot(sid, hist)
    except Exception as e:
        print(f"[DeckMeido] 会话 {sid} 落盘失败：{e}", file=sys.stderr)


def _safe_clear_stream(sid: str) -> None:
    """清流式残留：失败只是下次启动多恢复一条「中断回答」，不该打断收尾"""
    try:
        clear_stream(sid)
    except Exception as e:
        print(f"[DeckMeido] 清理流式残留失败（{sid}）：{e}", file=sys.stderr)


async def handle_chat(text: str, announce: bool = True,
                      image: str | None = None,
                      image_prompt: str = "（这是当前游戏画面，结合画面回答用户的问题）",
                      workspace: Path | None = None,
                      client: str | None = None,
                      device: str | None = None,
                      brief: str = "",
                      turn_id: str = "") -> dict:
    """brief 是给界面看的短文案（可选）：截屏询问那套提示词有一百多字，
   插件面板长显会把整个面板撑开。它只随消息存下来供各端显示，
   真正发给模型、算进上下文的仍然是 text。"""
    global pending_action, _ctx_used
    # 本轮归属快照：整轮全程只认这份 sid/epoch，中途别的端新建/切换会话也不影响它
    turn_sid = store.current_sid
    turn_epoch = store.epoch
    store.turn.update({"sid": turn_sid, "epoch": turn_epoch, "active": True,
                       "turn_id": turn_id,
                       # 本轮开始时刻：/api/status 的 running_since 用它，
                       # 界面显示耗时，也用来确认后端没死
                       "started": time.time(),
                       # 谁提的这一轮：危险操作挂起时要跟着记下，确认时只认它
                       "client": client or "", "device": device or ""})
    # 每轮开头强制复位一次遮罩状态：上一轮若在取消/异常路径里漏了
    # set_overlay(False)，这里不清就会一直盖着；passthrough 漏了就反过来，
    # 遮罩会一直藏着不显示（用户就没了终止入口），所以两个都复位
    if _overlay_state.get("show"):
        set_overlay(False)
    _overlay_state["passthrough"] = False
    # 本轮使用的历史对象。会话操作已改成「换对象」而不是原地清空，
    # 所以这里拿到的引用在被切走之后依然只属于这一轮
    hist = store.history
    sid_tag = {"sid": turn_sid}
    # 发起端自带的 turn_id 原样回显到这一轮的每条广播上：sid 是整个会话共用的，
    # 别的端提问时 sid 一样，发起端没法靠它认出「哪些 δ 是我这一轮的」
    if turn_id:
        sid_tag["turn_id"] = turn_id
    # 图片解析失败不能静默丢掉：用户以为「发了张图」，模型却只收到了文字
    image_blob = _pack_image(image)[1] if image else None
    if image and not image_blob:
        return {"reply": "附带的画面没能解析成图片（只支持 PNG / JPEG / WebP），这一轮没有发送。",
                "events": []}
    # 同一次调用取一个时间戳，同时用于「广播给界面的 ts」和「历史里的 ts」。
    # 前端删除消息时靠这个 ts 找回后端对应的那条：两处各自 time.time() 会差几毫秒，
    # 按 ts 精确匹配就永远对不上，删除按钮会退化成「删最后一条同角色消息」
    user_ts = time.time()
    # 来源标记：网页/手机/桌面客户端/QAM 插件各标各的，同一条对话里混着来
    # 也能看清谁说的。只挂在 user 消息上，且为空时不写字段，老数据零影响。
    # device 是每台浏览器各自的身份：client 只说「哪种浏览器」，两台电脑都是 web，
    # 分不清「这条是不是我这台发的」，所以「本端」角标要靠 device 比对
    src_tag = {"client": client} if client else {}
    if device:
        src_tag["device"] = device
    # 只给界面看的短文案：历史里留着，各端渲染时优先用它
    if brief:
        src_tag["brief"] = brief
    # 随本轮附带的画面（Decky 插件截的游戏画面）先落成文件：各端回放历史时会跳过
    # 「content 是数组」的消息（那是给模型看图的素材），只靠内联 base64 的话
    # 屏幕上永远没有图，表现就是「答得头头是道，却不知道它看的是什么」
    shot_event = _save_incoming_shot(image_blob) if image_blob else None
    if announce:
        # 画面要先落盘再广播：各端收到 user_message 时如果没带图，
        # 这一轮的画面就一直空着，只有刷新历史才补得回来
        await manager.broadcast({"type": "user_message", "text": text, "ts": user_ts,
                                 **sid_tag, **src_tag,
                                 **({"events": [shot_event]} if shot_event else {})})
        feed_push("user_message", text=text)
    # @{文件} 展开为内容；图片标记在 run_agent 里转成多模态 content
    # 画面在历史里只留一条 [SHOT:文件名] 标记（见 _shot_ref_content）。
    # 标记挂在 llm 字段上；content 仍然只有文字 —— 各端渲染的是 content，
    # 于是插件截的图和网页截的图在界面上走同一条路
    shot_ref = f"\n[SHOT:{shot_event['file']}]" if shot_event and shot_event.get("file") else ""
    hist.append({"role": "user", "content": text, "llm": _expand_context(text, workspace) + shot_ref,
                 "ts": user_ts, **src_tag,
                 **({"events": [shot_event]} if shot_event else {})})
    # 本轮新截的这张放在 llm 标记里（_build_llm_msgs 现读现编码），
    # 不再另补一条内联 base64 的消息：那条一旦被上下文压缩裁掉，界面上的图就没了
    events: list[dict] = []
    # run_agent 在终止时会往这里塞本轮执行过的工具，供落盘时做交接（见 _aborted_note）
    tool_log: list[dict] = []
    # 标题取自第一条用户消息，所以用户一发出就落盘并刷新侧栏，不必等整轮回答结束
    _safe_save(turn_sid, hist)
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})

    # 上下文逼近模型窗口上限时自动压缩（摘要替换旧对话，不再直接丢弃）。
    # _ctx_used 在切换会话等路径上会归零，这时按当前历史重新估算一次，
    # 否则会以为「才用了 0 token」而漏掉该压的情况；压缩失败也不能让这一轮起不来
    try:
        if _need_compact(_ctx_used or _estimate_tokens(
                _build_llm_msgs(hist, SYSTEM_PROMPT, turn_sid))):
            await compact_history(auto=True, hist=hist)
    except Exception as e:
        print(f"[DeckMeido] 自动压缩失败，按原上下文继续：{e}", file=sys.stderr)

    try:
        # 遮罩不再整轮常显：只有真的会动屏幕的工具（desktop 注入、xdotool/ydotool
        # 一类命令、拉起可见窗口）才在屏幕最上层盖一层，给用户一个「随时点一下终止」
        # 的入口 —— 那时候用户的手没法直接按终止。翻文件、跑后台命令、查天气这些
        # 不需要看屏幕的活儿不再挡视线，省得连「清空回收站」都要盖住全屏。
        # 遮罩的起落就在下面调用工具的前后（见 tools.touches_screen），这里不再常开。
        if not llm.configured:
            reply = "还没有配置模型。点右上角「设置」填好 API 地址和 Key 就能开始对话。"
        elif pending_action and not _pending_matches_turn(
                pending_action, turn_sid, turn_epoch, turn_id, client, device):
            # 挂起的操作不是这个会话/这个端提的：既不能替别人确认，也不该
            # 因为别处的对话把这边的手头操作清掉，按普通消息继续
            reply = await _run_agent_guarded(events, hist=hist, image_part=shot_ref,
                                             tool_log=tool_log, client=client)
        elif pending_action and tools.is_confirm(text):
            # 用户确认了挂起的危险操作
            action, pending_action = pending_action, None
            events.append({"type": "tool", "name": action["name"],
                           "summary": action["description"], "files": [], "approved": True})
            result = await tools.execute(action["name"], action["args"])
            tool_log.append({"name": action["name"], "summary": action["description"]})
            reply = f"已完成：{action['description']}\n{result}"
        elif pending_action:
            # 有新消息但未确认，取消挂起操作
            action, pending_action = pending_action, None
            reply = f"已取消：{action['description']}。\n\n" + await run_agent(
                events, hist=hist, image_part=shot_ref, tool_log=tool_log,
                from_client=client)
        else:
            # 画面要一路传进 run_agent：插件端才知道这一轮不用自己再截图
            reply = await _run_agent_guarded(events, hist=hist, image_part=shot_ref,
                                             tool_log=tool_log, client=client)
    except asyncio.CancelledError:
        # 用户点了终止：广播终止消息并向上传递
        await manager.broadcast({"type": "assistant_message", "text": "已终止当前操作",
                                 "ts": time.time(), "events": events, "cancelled": True,
                                 **sid_tag, **src_tag})
        feed_push("assistant_message", text="已终止当前操作", events=events)
        # 终止时把已经生成的部分保住：屏幕上出现过的每一段正文都留下，
        # 不能只用最后那段（工具调用前的过渡话也在屏幕上显示过）
        partial = _stream_buf(turn_sid).get("text") or ""
        parts = _stream_buf(turn_sid).get("parts") or []
        killed_segs = [p.get("content") or "" for p in parts if p.get("type") == "text"]
        killed_text = "\n\n".join(s.strip() for s in killed_segs if s.strip()).strip()
        if partial.strip() and partial.strip() not in killed_text:
            killed_text = (killed_text + "\n\n" + partial.strip()).strip() if killed_text else partial.strip()
        # 无论有没有留下正文，都要落一条带 aborted_turn 的 assistant 消息：
        # 它带着这一轮做过哪些工具，下一轮靠它告诉模型「已经终止，别接着做」
        # （见 _aborted_note）。以前只在有正文时才落，于是刚开跑就被终止的
        # 那一轮在历史里不留痕迹，模型下一句就照旧把半截任务续上
        hist.append({"role": "assistant", "content": killed_text,
                     "events": events, "interrupted": True, "aborted_turn": True,
                     "tool_log": list(tool_log), "ts": time.time(),
                     **src_tag})
        _safe_save(turn_sid, hist)
        _safe_clear_stream(turn_sid)
        raise
    except Exception as e:
        # 非终止类异常（网络、磁盘、工具内部炸了）：以前这里直接往上抛，
        # 把回复落盘、清 _streaming、复位 active 三步全跳过 —— 于是这一轮
        # 只留下用户那句话，屏幕上出现过的工具链全丢，而且 turn["active"]
        # 一直是 True，后面所有前端的「本轮归属」判定都跟着错。
        # 统一在这里收成一条带错误说明的回复，走与成功路径相同的收尾
        print(f"[DeckMeido] 本轮异常：{e}", file=sys.stderr)
        reply = f"（出错了：{e}）"
        if events:
            reply += "\n\n（上面这些步骤已经执行，结果未完整汇总）"
    finally:
        set_overlay(False)

    # 助手消息同样只取一次时间戳：落盘用的和广播给界面的是同一个值，
    # 前端按 data-ts 删除才能精确命中这一条
    asst_ts = time.time()
    # 回答也带上这条提问的来源：QAM 面板把各端的问答混在一列显示，
    # 只看 user 的标记分不清哪条回答是应哪一句的
    hist.append({"role": "assistant", "content": reply, "events": events,
                 "ts": asst_ts, **src_tag})
    # 关键：写回「本轮所属」的会话文件。中途被切走时 current_sid 已经指向别处，
    # 用 save_current_session 会把这一轮错写进新会话（串回复的根源）
    _safe_save(turn_sid, hist)
    # 正文已正式进 history，清掉流式残留（否则下次启动会多恢复出一条重复的「中断回答」）
    _safe_clear_stream(turn_sid)
    detached = _turn_detached(turn_epoch)
    store.turn["active"] = False
    turn_deleted = turn_sid in store.deleted_sids
    if turn_deleted:
        # 这一轮跑到一半会话被删了：结果按用户意愿丢弃，但要明确告知，
        # 否则用户看到的是「回答消失了」
        await manager.broadcast({"type": "system", "text": "该会话已被删除，本轮回答未保存",
                                 "ts": time.time()})
        feed_push("system", text="该会话已被删除，本轮回答未保存")
    if not detached and not turn_deleted and reply.strip():
        # 给前端一次「最终正文」：带 final=True，前端据此把那句界面上的气泡存进
        # localStorage 崩溃缓冲。中途被截断的工具过渡话不带这个标记，回滚时不会残留
        await manager.broadcast({"type": "delta", "text": "", "final": True,
                                 "reply": reply, "ts": time.time(), **sid_tag})
    if detached:
        # 本轮结束时用户已经切到别的会话：结果留在原会话文件里，不往当前屏幕推
        # （否则新会话里会冒出一段不属于它的回答），只提示一句
        await manager.broadcast({"type": "system", "text": "上一轮对话已完成，结果保存在原会话中",
                                 "ts": time.time()})
        feed_push("system", text="上一轮对话已完成，结果保存在原会话中")
    else:
        await manager.broadcast({"type": "assistant_message", "text": reply, "ts": asst_ts,
                                 "events": events, **sid_tag, **src_tag})
        feed_push("assistant_message", text=reply, events=events)
        # 语音播报：只播报最终回复，不播报思维链/工具过程（后台进行，不阻塞）
        asyncio.create_task(tts.speak(reply))
    return {"reply": reply, "events": events}


def _need_compact(used: int) -> bool:
    """用量是否已经该压缩了。

    两处触发（每轮开始前、单轮工具循环中间）共用这一个判断，避免阈值语义分叉。
    """
    win = context_window()
    return bool(win) and used / win >= COMPACT_RATIO


def _recent_limit() -> int:
    """一次请求里最多带多少条历史

    旧版 DeepSeek 接口有「单次最多 64 条 message、超出静默丢弃」的限制，
    现在这条限制已经取消，按 token 压缩就够了，不必为了凑条数去砍历史——
    砍掉中间一段反而会让后面的前缀全部失配（缓存直接掉到 0）。
    这里仍留一个宽松上限兜底，防止超长工具循环把请求撑到离谱。
    """
    return 200


def _compact_end(hist: list[dict], start: int) -> int:
    """算出这次压缩要把游标推进到第几条（即 end：history[start:end] 会被摘要）

    从末尾往前累积 token，一直收到「剩下的最近内容约占窗口的
    COMPACT_TARGET_LEFT」为止。这样压缩完上下文会明显低于触发阈值，
    而不是像以前那样只压掉几条、下一轮工具调用又顶回去。
    最近 2 条（用户那句 + 上一条）永远保留，避免新一轮刚开始就没有前文。

    条数上限按「请求里最多带多少条」反推（见 _recent_limit）：超过上限的消息
    DeepSeek 会直接忽略、而且不报错，表现就是「它突然忘了刚才说的事」。
    所以宁可多压一点，也不能让留给模型的那段被服务端悄悄截掉。
    """
    total = len(hist)
    if total - start <= 2:
        return total
    budget = int(context_window() * COMPACT_TARGET_LEFT)
    acc = 0
    end = total
    # 从最新一条往前累，直到快超出剩余额度为止。
    # 位置上限 = 请求条数上限减去「本轮新加的工具往返」的余量
    oldest = max(start, total - _recent_limit())
    for i in range(total - 1, oldest, -1):
        acc += _estimate_tokens([hist[i]])
        if acc > budget:
            break
        end = i
    end = max(end, start + 1)
    # 游标不能落在一次工具调用中间：assistant(tool_calls) 被压进摘要、它的
    # tool 结果还留在原文（或反过来），发出去的 messages 就是半截配对，接口 400。
    # 一路往后挪到「不是 tool 结果、也没有 tool_calls」那条消息上，整对一起压掉
    while start < end < total and (hist[end].get("role") == "tool"
                                   or hist[end].get("tool_calls")):
        end += 1
    return min(end, total)


async def compact_history(auto: bool = False, hist: list[dict] | None = None,
                          sid: str = "") -> str:
    """把较早的对话压缩成摘要，**只影响发给模型的上下文，不动界面历史**

    旧实现 hist[:] = [摘要] + hist[-4:] + 立刻落盘 —— 界面和磁盘上的几十轮
    对话被一起抹掉，用户看到的就是「内容凭空消失」。现在只推进压缩游标：
    摘要与 upto 存进文件，history 保持完整，重启后界面能一条不少地回放。
    """
    global _ctx_used
    hist = store.history if hist is None else hist
    sid = sid or store.turn.get("sid") or store.current_sid
    comp = _compact_for(sid)
    # 已经压缩过的部分不再重复参与摘要。
    #
    # keep 以前写死 4 条，后果是「刚压缩完马上又要压缩」：一次工具往返就占
    # assistant(tool_calls) + tool 两条，压缩后只剩 4 条的余量，下一轮工具链
    # 立刻把上下文顶回阈值，于是用户每几条消息就看到一次「已自动压缩上下文」。
    # 现在按 token 算：从末尾往前留出约 COMPACT_KEEP_RATIO 的窗口，
    # 压缩要真的腾出空间，压缩频率自然就降下来了。
    start = min(comp["upto"], len(hist))
    end = _compact_end(hist, start)
    old = hist[start:end]
    if len(old) < 2:
        return "对话还很短，无需压缩"
    if not llm.configured:
        return "未配置 LLM，跳过上下文压缩（历史原样保留）"
    # 老摘要一起喂进去，让新摘要接着往下写，否则前情会丢
    parts = []
    if comp["summary"].strip():
        parts.append("【前情摘要】\n" + comp["summary"])
    parts.append("【新增对话】\n" + "\n".join(
        # 每条按固定长度截断：摘要请求不带 tools、system 也固定，
        # 只要游标是从同一位置往后压，前缀就能和上一轮对齐（DeepSeek 硬盘缓存）
        f"{'用户' if m['role'] == 'user' else '助手'}: {_digest_text(m)[:500]}" for m in old))
    digest = "\n\n".join(parts)
    sys_msg = {"role": "system", "content": (
        "把下面的对话压缩成一份简洁摘要（中文），保留：用户的目标/偏好、"
        "已完成的操作及结果、待办事项、关键文件路径。直接输出摘要正文。")}
    try:
        summary = await llm.chat([sys_msg, {"role": "user", "content": digest}])
        # 弱模型有时只回一句「用户询问了某问题」，那么长的历史一压就没了。
        # 摘要短到不成比例时补一次：明确指出漏了哪些栏位，仍不合格就用原文兜底。
        if len(summary.strip()) < max(120, len(digest) // 20):
            retry_sys = {"role": "system", "content": (
                "上一次的摘要太简略，丢掉了很多信息。请重新压缩，必须分段写清："
                "① 用户的目标与偏好 ② 已执行的操作及结果（含命令与文件路径）"
                "③ 未完成的待办 ④ 已知的结论与报错。不要评论，直接输出摘要正文。")}
            again = await llm.chat([retry_sys, {"role": "user", "content": digest}])
            if len(again.strip()) > len(summary.strip()):
                summary = again
    except Exception as e:
        return f"压缩失败，历史原样保留：{e}"
    if sid in store.deleted_sids:
        # 会话在压缩过程中被删了：游标与落盘一律作废，否则删掉的会话会被写回来
        return summary
    _set_compact(sid, summary, end)
    _ctx_used = _estimate_tokens(_build_llm_msgs(hist, SYSTEM_PROMPT, sid))
    # 游标要和会话文件一起持久化，否则重启后模型会重新吃到全量历史
    if sid == store.current_sid:
        save_current_session()
    else:
        save_session_snapshot(sid, hist)
    note = (f"{'已自动压缩上下文' if auto else '已压缩上下文'}"
            f"（{len(old)} 条旧消息 → 摘要，聊天记录完整保留）")
    await manager.broadcast({"type": "system", "text": note, "ts": time.time()})
    feed_push("system", text=note)
    return summary


@app.post("/api/compact", dependencies=[Depends(check_token)])
async def api_compact():
    summary = await compact_history()
    return {"ok": True, "summary": summary[:300]}


def _sd_card_info() -> dict:
    """内存卡占用：找 /dev/mmcblk* 分区（SteamOS 自动挂在 /run/media/<用户>/<卷名>）

    没插卡就返回空 dict，前端据此整行隐藏。
    """
    import psutil
    try:
        parts = psutil.disk_partitions(all=True)
    except Exception:
        return {}
    for part in parts:
        if not part.device.startswith("/dev/mmcblk"):
            continue
        try:
            du = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        mount = part.mountpoint.rstrip("/")
        return {
            "sd_label": os.path.basename(mount) or mount,
            "sd_mount": mount,
            "sd_pct": du.percent, "sd_used": du.used, "sd_total": du.total,
        }
    return {}


@app.get("/api/sysinfo", dependencies=[Depends(check_token)])
async def api_sysinfo():
    """侧栏状态卡：CPU / 内存 / SSD / 内存卡占用 / 电量 / 运行时长 / 局域网 IP

    采集全程要读 /proc、遍历挂载点、还要 fork 一次 `ip`，放到线程里做：
    这个接口是 5 秒一刷的，同步跑会把事件循环卡住。
    """
    return await asyncio.to_thread(_read_sysinfo)


def _read_sysinfo() -> dict:
    try:
        import psutil
        vm = psutil.virtual_memory()
        data = {
            "cpu": psutil.cpu_percent(),
            "mem_pct": vm.percent, "mem_used": vm.used, "mem_total": vm.total,
            "uptime": int(time.time() - psutil.boot_time()),
            "ip": _lan_ip(),
            # 设备名：优先用户自己设的 hostname（SteamOS 默认 deck@steamdeck）
            "hostname": socket.gethostname(),
        }
        # 内置盘：SteamOS 根分区只读且很小，用户数据都在 /home，看它才有意义
        try:
            du = psutil.disk_usage("/home")
            data.update({"ssd_pct": du.percent, "ssd_used": du.used, "ssd_total": du.total})
        except OSError:
            pass
        battery = read_first("/sys/class/power_supply/BAT1/capacity")
        charging = read_first("/sys/class/power_supply/BAT1/status")
        data["battery"] = int(battery) if battery.isdigit() else None
        data["charging"] = charging or None
        data.update(_sd_card_info())
        return data
    except Exception as e:
        return {"error": str(e)}


class OpenFileReq(BaseModel):
    path: str


@app.post("/api/open_file", dependencies=[Depends(check_token)])
async def api_open_file(req: OpenFileReq):
    """原生客户端里点开图片/文件 → 用系统默认工具打开（仅限 static 目录内）"""
    rel = req.path.split("?")[0].lstrip("/")
    root = STATIC_DIR.resolve()
    p = (root / rel).resolve()
    # 按路径层级判断，不能拼字符串：/static-other/x 的 startswith("/static") 也成立
    if not (p == root or root in p.parents) or not p.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    subprocess.Popen(["xdg-open", str(p)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ---------- 会话管理 ----------

@app.get("/api/sessions", dependencies=[Depends(check_token)])
async def api_sessions():
    items = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            items.append({"id": d.get("id", p.stem), "title": d.get("title", "会话"),
                          "updated": d.get("updated", 0), "messages": len(d.get("history", []))})
        except Exception:
            continue
    return {"current": store.current_sid, "items": items}


def _current_session_untouched() -> bool:
    """当前会话是不是「一句话都没说过」的空会话

    判据是历史里一条 user / assistant 消息都没有（系统提示、tool 消息不算）。
    只看 store.history 而不是磁盘：内存里的历史就是唯一真相，磁盘文件
    可能因为空写保护还没落盘。
    """
    return not any(m.get("role") in ("user", "assistant") for m in store.history)


@app.post("/api/sessions/create", dependencies=[Depends(check_token)])
async def api_session_create():
    global _ctx_used
    save_current_session()
    # 连点「新会话」会把侧栏刷成一排空会话，一个能用的都没有。
    # 已经在空会话里就原地不动，各端也不用重刷列表
    if _current_session_untouched():
        return {"ok": True, "id": store.current_sid, "unchanged": True}
    store.current_sid = _fresh_sid()
    # 换对象而不是原地 clear：还在跑的那一轮持有旧列表引用，
    # 原地清空会把它的历史一起抹掉，结果就变成「有问无答」
    store.history = []
    # 上下文占用计数必须一起清零，否则新会话会沿用上一个会话的用量
    _ctx_used = 0
    # 代次 +1：正在跑的那一轮会据此发现自己的会话已被换掉
    store.bump_epoch()
    store.turn["active"] = False
    save_current_session()
    await manager.broadcast({"type": "history", "messages": store.history,
                             "sid": store.current_sid})
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    await manager.broadcast({"type": "system", "text": "已开始新会话", "ts": time.time()})
    feed_push("system", text="已开始新会话")
    return {"ok": True, "id": store.current_sid}


class SwitchReq(BaseModel):
    id: str


@app.post("/api/sessions/switch", dependencies=[Depends(check_token)])
async def api_session_switch(req: SwitchReq):
    global _ctx_used
    p = _sess_path(req.id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="session not found")
    save_current_session()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # 文件坏了就明确拒绝：以前这里直接抛 500，前端只能笼统提示「切换失败」，
        # 而用户看到的是会话列表里明明有这一条
        raise HTTPException(status_code=500,
                            detail=f"会话文件读取失败：{e}") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="会话文件格式异常")
    store.current_sid = data.get("id", req.id)
    # 同样换对象：在跑的那一轮仍持有旧列表，不受切换影响
    store.history = data.get("history", [])
    comp = data.get("compact")
    if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
        _set_compact(store.current_sid, comp.get("summary") or "",
                     min(int(comp.get("upto") or 0), len(store.history)))
    else:
        store.drop_compact(store.current_sid)
    _ctx_used = _estimate_tokens(
        _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
    store.bump_epoch()
    store.turn["active"] = False
    await manager.broadcast({"type": "history", "messages": store.history,
                             "sid": store.current_sid})
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    return {"ok": True}


class MsgDeleteReq(BaseModel):
    ts: float | None = None          # 按消息时间戳定位（前端每条消息都带 data-ts）
    role: str = "assistant"          # 兜底：没有 ts 时按角色删最后一条
    with_reply: bool = False         # 删用户消息时，连它下面那条回答一起删


@app.post("/api/messages/delete", dependencies=[Depends(check_token)])
async def api_message_delete(req: MsgDeleteReq):
    """删除会话里的单条消息。

    只从「当前会话的历史」里摘掉那一条，并立刻落盘；不发 history 全量重推，
    否则整屏会重绘、滚动位置跳到顶上。前端自己把 DOM 上的那条摘掉即可。

    为什么按 ts 定位而不是按序号：界面上的顺序和 history 不完全一一对应
    （一轮助手回答在界面上可能拆成 思考/正文/工具 好几块，但在 history 里
    只有一条），用序号会删错行。ts 是每条消息落盘时就写进去的，唯一且稳定。
    """
    global _ctx_used
    if store.turn.get("active"):
        raise HTTPException(status_code=409, detail="本轮回答还在生成，请等它结束再删除")

    hidx = -1
    if req.ts:
        # 时间戳浮点比较留一点容差：JSON 往返可能丢精度
        best = None
        for i, m in enumerate(store.history):
            t = m.get("ts")
            if not isinstance(t, (int, float)):
                continue
            if abs(float(t) - float(req.ts)) <= 0.001:
                best = i
                break
        hidx = best if best is not None else -1
    if hidx < 0:
        # 没有匹配到 ts：退化成「删同角色最后一条」，避免前端按钮完全失效
        want = req.role if req.role in ("user", "assistant") else "assistant"
        for i in range(len(store.history) - 1, -1, -1):
            if store.history[i].get("role") == want:
                hidx = i
                break
    if hidx < 0:
        raise HTTPException(status_code=404, detail="找不到这条消息")

    target = store.history[hidx]
    removed = 1
    # 删用户消息时，把紧跟着的那条助手回答一起带走：
    # 只删提问留着回答，模型下次读历史会看到「没有问题的答案」，很容易胡接
    if req.with_reply and target.get("role") == "user":
        if hidx + 1 < len(store.history) and store.history[hidx + 1].get("role") == "assistant":
            del store.history[hidx + 1]
            removed += 1
    del store.history[hidx]

    # 压缩游标要跟着收：否则游标指向的条数比历史还长，
    # 发给模型的上下文会整段错位（摘要跳过了还没被摘要的消息）
    sid = store.current_sid
    comp = _compact_for(sid)
    if comp["upto"] > len(store.history):
        _set_compact(sid, comp["summary"], len(store.history))

    save_current_session()
    _ctx_used = _estimate_tokens(
        _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
    await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    return {"ok": True, "removed": removed, "left": len(store.history)}


@app.delete("/api/sessions/{sid}", dependencies=[Depends(check_token)])
async def api_session_delete(sid: str):
    global _ctx_used
    # 先拉黑再删文件：反过来的话，删完到拉黑之间那一瞬间的落盘（流式回调）
    # 就能把文件写回来，等于白删
    store.deleted_sids.add(sid)
    p = _sess_path(sid)
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            store.deleted_sids.discard(sid)
            raise HTTPException(status_code=500, detail=f"删除失败：{e}")
    if sid == store.current_sid:
        # 删掉的是当前会话。以前这里无条件 _fresh_sid() 再落盘一个空会话，
        # 于是「删一个」必然「多一个」；现在先试着切到剩下最新的那个，
        # 真的一个都不剩了才新建（且标题带时间，不再和别的「新会话」重名）
        store.bump_epoch()
        store.turn["active"] = False
        rest = []
        for f in sorted(SESSIONS_DIR.glob("*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (d.get("id") or f.stem) in store.deleted_sids:
                continue
            rest.append(d)
        nxt = next((d for d in rest if d.get("history")), None)
        if nxt is None:
            store.current_sid = _fresh_sid()
            store.history = []
            _ctx_used = 0
            save_current_session()
        else:
            store.current_sid = nxt.get("id") or _fresh_sid()
            # 换对象而不是原地 clear：在跑的那一轮仍持有旧列表引用
            store.history = nxt.get("history") or []
            comp = nxt.get("compact")
            if isinstance(comp, dict) and (comp.get("summary") or comp.get("upto")):
                _set_compact(store.current_sid, comp.get("summary") or "",
                             min(int(comp.get("upto") or 0), len(store.history)))
            else:
                store.drop_compact(store.current_sid)
            _ctx_used = _estimate_tokens(
                _build_llm_msgs(store.history, SYSTEM_PROMPT, store.current_sid))
        await manager.broadcast({"type": "history", "messages": store.history,
                                 "sid": store.current_sid})
        await manager.broadcast({"type": "session_changed", "current": store.current_sid})
    return {"ok": True, "current": store.current_sid}


# 旧接口兼容：新会话
@app.post("/api/session/new", dependencies=[Depends(check_token)])
async def api_new_session():
    return await api_session_create()


@app.post("/api/checkpoint", dependencies=[Depends(check_token)])
async def api_checkpoint():
    """客户端退出前先来这儿「打个招呼」：把还没落盘的东西全部刷下去。

    以前客户端退出直接 SIGTERM，后端跑着的这一轮回答就整段消失。
    现在客户端先调这里（拿到 ok 后再发 SIGTERM），正在生成中的正文至少保住到此刻。
    """
    flushed = False
    try:
        buf = _stream_buf()
        text = buf.get("text") or ""
        if store.turn.get("active") and text.strip():
            persist_stream(text, force=True, parts=buf.get("parts") or [])
            flushed = True
    except Exception:
        pass
    # 只刷「还没落盘的流式正文」，不要拿内存 history 覆盖文件：
    # 用户消息在 handle_chat 开头就落盘了，正文在收尾时落盘，内存里的东西不比文件新。
    # 反过来覆盖一次就可能把文件里的历史冲掉（内存为空/偏旧时会直接清空会话）。
    return {"ok": True, "flushed": flushed, "sid": store.current_sid,
            "turns": len(store.history)}


_lan_cache: tuple[float, str] = (0.0, "")
_lan_lock = threading.Lock()


def _lan_ip() -> str:
    """取真实局域网 IP：枚举网卡，优先 192.168.x，避开 VPN/加速器 TUN 虚拟段

    结果缓存 60 秒：这个函数每次都要 fork 一次 `ip`，而状态卡是 5 秒一刷的。
    锁只护住缓存读写那几行，真正的 `ip` 调用仍在锁外跑：多端同时刷状态时
    最多多 fork 几次，绝不因为一个网卡探测把整个事件循环堵住。
    """
    global _lan_cache
    now = time.monotonic()
    with _lan_lock:
        if _lan_cache[1] and now - _lan_cache[0] < 60:
            return _lan_cache[1]
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3).stdout
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    except Exception:
        ips = []

    # 只在 RFC1918 私有段里挑：198.18.x 是 Clash/加速器的 fake-ip 段、
    # 169.254.x 是本地链接保留段，都不是别的设备能连上的局域网地址，
    # 混进来会让状态卡显示一个「代理地址」而不是真实局域网 IP。
    ips = [a for a in ips if a.startswith("192.168.") or a.startswith("10.")
           or re.match(r"172\.(1[6-9]|2\d|3[01])\.", a)]

    def rank(a: str) -> int:
        if a.startswith("192.168."):
            return 0
        if a.startswith("10."):
            return 1
        return 2

    ips.sort(key=rank)
    ip = ips[0] if ips else "127.0.0.1"
    with _lan_lock:
        _lan_cache = (now, ip)
    return ip


def phone_url() -> str:
    """手机扫码/直连用的完整地址（带 token 免输入）"""
    port = int(CFG["server"].get("port", 18765))
    return f"http://{_lan_ip()}:{port}/?token={TOKEN}"


@app.get("/api/qrcode", dependencies=[Depends(check_token)])
async def api_qrcode():
    """局域网访问地址 + 二维码 PNG（base64）"""
    url = phone_url()
    png_b64 = ""
    try:
        import base64
        import io

        import qrcode

        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return {"url": url, "qr": png_b64}


# ---------- 操作遮罩 ----------
# agent 真正操作设备时，桌面上盖一层半透明遮罩；用户点一下遮罩即终止当前操作

_overlay_proc = None
_overlay_state = {"show": False, "text": "", "passthrough": False, "lift": False, "pointer": False}
_overlay_ack = {"passthrough": False, "lift": False}  # 遮罩进程确认已让开鼠标 / 已抬高


def _xauthority() -> str:
    """当前可用的 X 授权文件

    Xwayland 每次会话都会换一个 xauth_XXXXXX 文件，后端进程环境里那份
    早晚会失效。这里先认环境里的路径（存在才算数），否则按修改时间取
    最新的一份；用 setdefault 是不够的——环境里留着旧路径时新文件永远
    不会被找到，遮罩会一直报 "Authorization required"。
    """
    cur = os.environ.get("XAUTHORITY", "")
    if cur and Path(cur).exists():
        return cur
    cands = glob.glob(f"/run/user/{os.getuid()}/xauth_*")
    cands = [c for c in cands if Path(c).exists()]
    if cands:
        return max(cands, key=lambda p: Path(p).stat().st_mtime)
    home = Path.home() / ".Xauthority"
    return str(home) if home.exists() else ""


def _overlay_env() -> dict:
    """桌面会话环境：systemd 用户服务里默认没有 DISPLAY / XAUTHORITY"""
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    auth = _xauthority()
    if auth:
        env["XAUTHORITY"] = auth
    else:
        env.pop("XAUTHORITY", None)
    env["DECKMEIDO_API"] = f"http://127.0.0.1:{CFG['server'].get('port', 18765)}"
    env["DECKMEIDO_TOKEN"] = str(CFG["server"].get("token", ""))
    # 走 Xwayland（xcb）：Wayland 原生窗口没有「不进任务栏」的协议，遮罩必须走 X11 才能隐藏
    env["QT_QPA_PLATFORM"] = "xcb"
    return env


_overlay_lock = threading.Lock()


def ensure_overlay() -> None:
    """遮罩进程常驻，按需显示；崩溃后自动重启

    用 --force 启动：先让可能残留的旧遮罩自己退位，再起新的一层。
    否则后端重启留下的孤儿遮罩会越积越多，操作时叠出好几层。

    起完之后要核对「这一层真的拿到套接字了」：overlay 的 force 接管
    先 ping 旧实例、再等它死透，接口正常时这一步不该失败；但万一旧实例
    卡在 X11 调用里没退，新进程会自己退出，此时 _overlay_proc 是个
    已死的句柄，遮罩层等于没起。核对一遍并补拉一次，避免「点了停止
    没反应」这种更难受的故障。

    这个函数里有最长 6 秒的等待（等旧实例退位），所以异步调用点必须用
    asyncio.to_thread 包起来，不能直接在事件循环里调。
    """
    # 两轮对话同时开始工具调用时，别各自拉起一层遮罩
    with _overlay_lock:
        _ensure_overlay_locked()


def _ensure_overlay_locked() -> None:
    global _overlay_proc
    if _overlay_proc is not None and _overlay_proc.poll() is None:
        return

    def spawn() -> bool:
        """拉起一层遮罩并确认它活着且有套接字"""
        log = open(BASE_DIR / "overlay.log", "ab")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "overlay.py"), "--force"],
                cwd=str(BASE_DIR), env=_overlay_env(),
                stdout=log, stderr=log, start_new_session=True,
            )
        except Exception:
            return False
        finally:
            try:
                log.close()
            except Exception:
                pass
        # 接管要等旧实例退位，给足时间；退出码非 0 说明没抢到
        for _ in range(40):          # 最多 6 秒
            if proc.poll() is not None:
                _ = proc.poll()      # 非 0 = 没接管成功
                return proc.returncode == 0
            time.sleep(0.15)
        globals()["_overlay_proc"] = proc
        return True

    for _ in range(2):               # 失败一次再补一次
        if spawn():
            return



def stop_overlay() -> None:
    """服务退出时收掉自己拉起的遮罩，避免留下孤儿进程

    两处兜底，都是踩过的坑：
    1. 只 terminate 不 kill：遮罩若卡在 X11 调用里不响应 SIGTERM，
       这个同步 wait 就会一直占着 AnyIO worker 线程，把整个后端的
       shutdown 拖死——uvicorn 收不到中断，端口释放不掉，下次启动
       要么被单实例守卫挡回，要么去抢 socket。所以超时后必须 kill。
    2. 兜底按 pid 清理：进程句柄可能已经丢了（后端重启过），此时靠
       _overlay_proc 一个都收不到，孤儿就留在桌面上继续画遮罩。
    """
    global _overlay_proc
    proc, _overlay_proc = _overlay_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
    # 句柄之外的残留（含拿不到套接字的聋子孤儿）统一清掉
    for pid in _overlay_pids():
        try:
            if proc is not None and pid == proc.pid:
                continue
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    # 关键：僵尸子进程必须回收（waitpid）。
    # overlay.py 被 --force 接管时会被别人 SIGKILL，它成了僵尸挂在 CMD_START 的
    # 事件循环里没被 wait 过；若不 reap，主进程被 SIGTERM 后 uvicorn 的 shutdown
    # 会一直卡在「Waiting for background tasks to complete」，端口不释放，
    # 表现为一个既不服务也不退出的幽灵后端（还会被单实例守卫挡着不让重启）。
    # 只回收我们自己那个 pid：waitpid(-1) 会把 asyncio 管着的命令进程一起摘走
    if proc is not None:
        _reap_children(proc.pid)


def _reap_children(only_pid: int | None = None) -> None:
    """回收已退出但还没 wait 的子进程（僵尸）。

    `waitpid(-1)` 会摘走**任意**一个子进程 —— 包括 asyncio 子进程管理器
    正在管着的 run_command 进程。那些进程的退出码需要由事件循环回收，
    被这里抢走后 asyncio 会一直等不到终止状态，命令永远「不结束」。
    所以只回收我们自己拉起的那个 pid（遮罩），没有指定就什么都不做。
    """
    if only_pid is None:
        return
    try:
        os.waitpid(only_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _overlay_pids() -> list[int]:
    """找出本机所有 overlay.py 进程（含孤儿）"""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", str(BASE_DIR / "overlay.py")],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.split():
            if line.strip().isdigit():
                pid = int(line)
                if pid != os.getpid():
                    pids.append(pid)
    except Exception:
        pass
    return pids



def set_overlay(show: bool, text: str = "") -> None:
    _overlay_state["show"] = show
    if text:
        _overlay_state["text"] = text
    if not show:
        _overlay_state["since"] = 0.0
        # 不要在这里顺手把 passthrough 清掉：可能正好有一次点击注入在跑，清了之后
        # 遮罩会重新冒出来挨这一下点击，于是又被当成「用户终止」。passthrough 只由
        # set_overlay_passthrough 收
        # 遮罩收起：不再需要为它压着电源抑制（命令还在跑的话那条原因仍在，
        # 计数归零才真的释放，见 power.AwakeKeeper）
        power.keeper.release("overlay")
    else:
        # 记下「从什么时候开始要它亮」。看门狗据此判断是不是有哪条异常路径
        # 漏了 set_overlay(False)，把遮罩永远留在屏幕上。
        if not _overlay_state.get("since"):
            _overlay_state["since"] = time.time()
        # 遮罩亮着 = 用户可能正盯着看操作过程，别在这时候息屏
        power.keeper.hold("overlay", text or "正在思考")
        ensure_overlay()


async def set_overlay_async(show: bool, text: str = "") -> None:
    """set_overlay 的异步版本：拉起遮罩那段（最长 6 秒）丢到线程里做

    直接 await 这个而不是调 set_overlay：后者会 fork 进程并轮询等它接管，
    在事件循环里跑会把所有 ws 广播和心跳按住。
    """
    if not show:
        set_overlay(False, text)
        return
    _overlay_state["show"] = True
    if text:
        _overlay_state["text"] = text
    if not _overlay_state.get("since"):
        _overlay_state["since"] = time.time()
    power.keeper.hold("overlay", text or "正在思考")
    await asyncio.to_thread(ensure_overlay)


# 遮罩最多连亮这么久；超过就认为状态没被复位（异常/取消路径逃逸），强制收掉。
# 一整轮最长思考时间可能有一两分钟（多轮工具 + 模型推理），这里给足余量；
# 正常结束时 handle_chat 的 finally 会收掉，用不到这个兜底。
_OVERLAY_MAX_HOLD = 300.0


def _overlay_watchdog() -> None:
    """兜底线程：遮罩亮着太久就强制收起，别让它一直挡住屏幕。

    遮罩在「会动屏幕」的工具执行期间常显（见 tools.touches_screen），正常收尾由
    调用方 set_overlay(False)；取消、异常、工具执行到一半被终止，任一环节漏掉，
    遮罩就会一直盖在那儿。这里按 _OVERLAY_MAX_HOLD 兜一个上限。
    """
    while True:
        time.sleep(1.0)
        try:
            if not _overlay_state.get("show"):
                continue
            since = _overlay_state.get("since") or 0.0
            if since and (time.time() - since) > _OVERLAY_MAX_HOLD:
                print("[DeckMeido] 遮罩超时未复位，已强制收起", file=sys.stderr)
                set_overlay(False)
        except Exception:
            pass


threading.Thread(target=_overlay_watchdog, name="overlay-watchdog", daemon=True).start()


async def set_overlay_passthrough(on: bool) -> None:
    """让遮罩暂时让开，使 agent 的点击能落到下面的窗口。

    遮罩窗口跑在 XWayland 里，Wayland 合成器做命中测试时看不见 X11 的
    input shape，所以「窗口标志让开」是不成立的；遮罩进程收到这个标志后
    会把窗口整块 hide()，并回报「确实藏好了」。这里必须等那个回报再放行，
    否则点击会打在还没撤下去的遮罩上，被当成「用户终止」。
    """
    _overlay_state["passthrough"] = on
    _overlay_ack["passthrough"] = False
    if not on:
        return
    if _overlay_proc is None or _overlay_proc.poll() is not None:
        return  # 遮罩没跑，不用等
    for _ in range(80):  # 最多等 800ms（hide + 合成器撤层需要几帧）
        if _overlay_ack["passthrough"]:
            return
        await asyncio.sleep(0.01)


tools.ON_OVERLAY_PASSTHROUGH = set_overlay_passthrough


def set_overlay_pointer(on: bool) -> None:
    """预告「下一步会按下鼠标键」，让遮罩别抢这一下点击。

    遮罩是 XWayland 窗口，非穿透状态下会把点击吃掉（=用户终止）。而
    set_overlay_passthrough 在遮罩还没显示时什么都不做，工具内部自己去调也来
    不及。所以这里把意图做成后端状态，遮罩进程看到 pointer 立刻转 25ms 快轮询，
    显示后马上 hide 并回报，agent 随后注入的点击就落在下面的窗口上。
    """
    _overlay_state["pointer"] = bool(on)


async def set_overlay_lift(on: bool) -> None:
    """截图期间把遮罩收起来，拍完再放回来。

    打开时要等遮罩真正隐藏（它每 25ms 轮询并回报 ack），否则这张截图里还是有遮罩。
    """
    _overlay_state["lift"] = on
    if not on:
        return
    if _overlay_proc is None or _overlay_proc.poll() is not None:
        return  # 遮罩没跑，不用等
    for _ in range(20):  # 最多等 200ms
        if _overlay_ack["lift"]:
            return
        await asyncio.sleep(0.01)


tools.ON_OVERLAY_LIFT = set_overlay_lift


@app.get("/api/controls", dependencies=[Depends(check_token)])
async def api_controls(force: int = Query(default=0)):
    """控件自检：force=1 时跳过缓存重新实测（网页上点徽章刷新用）

    自检里是一串 subprocess 探针，必须走线程，否则缓存过期的瞬间会把事件循环卡住。
    """
    return await asyncio.to_thread(depcheck.summary, bool(force))


@app.post("/api/controls/fix", dependencies=[Depends(check_token)])
async def api_controls_fix():
    """修复控件：只做不需要 root 的那半——把 ydotoold 交给 systemd 用户单元托管。

    装系统包需要 sudo 密码，而这里是 systemd 托管的后台进程，接不了终端输入，
    也没有可用的免密策略，所以一律引导用户到终端执行 install.sh。
    """
    out: dict = {"results": []}

    ok, msg = await asyncio.to_thread(depcheck.fix_ydotoold)
    out["results"].append({"key": "ydotoold", "ok": ok, "message": msg})
    out["need_sudo"] = True
    out["hint"] = ("安装系统控件需要 root 权限。请在终端执行："
                   "bash install.sh check --fix（会提示输入 sudo 密码）")

    # 走线程：刚点完修复就要再跑一遍全套探针，同步跑会把事件循环卡住
    out["controls"] = await asyncio.to_thread(depcheck.summary, True)
    await manager.broadcast({"type": "status", "data": get_status()})
    return out


@app.get("/api/overlay_state", dependencies=[Depends(check_token)])
async def api_overlay_state(ack: int = 0, hidden: int = 1):
    # 遮罩进程带 ack=1 轮询时，表示它已经按本次返回的状态生效过了。
    # hidden 是它自报的「当前窗口是否已隐藏」：要求让开时只有真的藏了才算确认，
    # 否则 agent 那一下点击仍可能打在还显示着的遮罩上，被当成用户终止
    if ack:
        # pointer 期间遮罩按「让开」处理，但它由后端先置、遮罩后看到，所以
        # 要不要等同样看它有没有真的藏：两个标志任一为真都按穿透算
        want = _overlay_state["passthrough"] or _overlay_state["pointer"]
        if want and not _overlay_state["lift"]:
            _overlay_ack["passthrough"] = bool(hidden)
        else:
            _overlay_ack["passthrough"] = want
        _overlay_ack["lift"] = _overlay_state["lift"]
    return _overlay_state


@app.on_event("startup")
async def _boot_overlay():
    ensure_overlay()


@app.on_event("shutdown")
async def _shutdown_overlay():
    # 服务被重启/停止时把自己拉起的遮罩一起收掉，别留孤儿
    stop_overlay()
    # 电源抑制也一起放掉，别让下一个进程/系统替我们背着
    try:
        power.keeper.shutdown()
    except Exception:
        pass
    # 退出前抢救一次：正在生成的正文与内存历史立刻落盘。
    # 客户端退出、systemctl restart、升级重启都走这条路径，
    # 少了这一步，被打断的那一轮回答就会整段消失（用户视角就是「记录丢了」）
    try:
        text = _stream_buf().get("text") or ""
        if store.turn.get("active") and text.strip():
            persist_stream(text, force=True, parts=_stream_buf().get("parts") or [])
            print(f"[DeckMeido] 退出前已保存未完成的回答（{len(text)} 字）", file=sys.stderr)
        # 这里刻意不写内存 history：正文在 handle_chat 收尾时已落盘，用户消息开头就落盘了，
        # 内存不比文件新。而「拿内存覆盖文件」在内存为空/偏旧时会直接把历史冲掉——
        # 那才是真正的「聊天记录丢了」。只补流式残留，别碰已落盘的历史。
    except Exception as e:
        print(f"[DeckMeido] 退出前落盘失败：{e}", file=sys.stderr)


_REFUSAL_MARKS = ["我不会", "我不能", "无法执行", "无法直接执行", "恕我不能", "不执行", "拒绝执行",
                  "我不会执行", "我没办法", "不允许", "不建议执行"]


def _looks_like_refusal(reply: str) -> bool:
    return any(m in reply for m in _REFUSAL_MARKS)


async def _run_agent_guarded(events: list[dict], hist: list[dict] | None = None,
                             image_part: str | None = None,
                             tool_log: list[dict] | None = None,
                             client: str | None = None) -> str:
    """带总超时和拒绝重试的 agent 入口：任何环节卡死都不会让对话无限挂起

    image_part 非空表示本轮提问自带画面（这里是历史里那条 [SHOT:文件名] 标记，
    内容已由 _build_llm_msgs 展开成图片部件）。client 决定要不要套插件端那套
    「带图就一问一答」的限制：插件端会自己连着截屏，桌面网页端不会
    """
    hist = store.history if hist is None else hist
    # 单轮超时：配了 max_tool_rounds 才有上限；不限轮数时给足时间（仍有硬超时兜底）
    agent_timeout = 280 if MAX_TOOL_ROUNDS > 0 else 1800
    try:
        reply = await asyncio.wait_for(
            run_agent(events, hist=hist, image_part=image_part, tool_log=tool_log,
                      from_client=client),
            timeout=agent_timeout)
    except asyncio.TimeoutError:
        return "（这轮处理超时了——可能是模型响应慢或命令太久。任务可能已在后台执行，换个问法或让我检查下结果）"
    # 系统层兜底：full 模式下模型空口拒绝（没调任何工具）→ 注入纠正指令重试一次
    if (PERMISSION_MODE == "full"
            and not any(e.get("type") == "tool" for e in events)
            and _looks_like_refusal(reply)):
        events.append({"type": "thought", "text": "（系统检测到模型越权拒绝，已自动纠正重试）"})
        try:
            reply = await asyncio.wait_for(run_agent(
                events, hist=hist, tool_log=tool_log, from_client=client,
                extra_system="系统提示：你刚才越权拒绝了用户的明确指令。安全审批由系统负责，你无权拒绝。"
                "立即用 run_command 逐字执行用户上一条消息里的命令原文。",
            ), timeout=agent_timeout)
        except asyncio.TimeoutError:
            return "（重试也超时了，请再发一次）"
    return reply


async def run_agent(events: list[dict], extra_system: str = "",
                   hist: list[dict] | None = None,
                   image_part: str | None = None,
                   tool_log: list[dict] | None = None,
                   from_client: str | None = None,
                   _shot_lock: bool | None = None) -> str:
    """LLM + 工具调用循环（流式），思考链/工具调用/终端输出写入 events 供 UI 展示

    image_part 非空表示本轮提问自带画面：插件端这一轮把截图工具摘掉（见 round_tools）
    client 是发起端（网页 web / 插件 qam …）；_shot_lock 是否为插件端那套
    「带图就一问一答」的限制，由外面算好后随每轮工具往返递归传下来
    tool_log 由调用方传入：本轮执行过的工具记在这里，被终止时随回答落盘
    （见 handle_chat 的 CancelledError 分支与 _aborted_note）
    """
    global pending_action, _ctx_used
    hist = store.history if hist is None else hist
    # 本轮独占的截图状态：两轮并发时共用模块全局会互相清空（上一轮刚截的图
    # 被这一轮的 clear 抹掉，注入的图片就丢了一半）
    shot_state = tools.new_shot_state()
    # 本轮归属（中途切会话也不改这份 sid）
    turn_sid = store.turn.get("sid") or store.current_sid
    # 当前模式必须实测后告诉模型：游戏模式用 gamepad 注入、游戏模式下从 gamescope
    # 抓帧、遮罩提示挂 gamescope 覆盖层；桌面模式走 desktop 点按 + spectacle 截图。
    # 两套操作手段不通用，猜错就是「点了没反应」或「抓不到画面」。探针会起子进程，
    # 所以放到线程里跑，别卡住这一轮
    gaming = await asyncio.to_thread(tools.mode_is_gaming)
    sys_prompt = SYSTEM_PROMPT + _mode_block(gaming) + (f"\n{extra_system}" if extra_system else "")
    # 长期记忆拼在最末尾（见 _memory_block 里的缓存说明）。每轮都重新取，
    # 用户刚在对话里让它「记住」，这一轮之后就能生效
    mem = _memory_block()
    if mem:
        sys_prompt += "\n\n" + mem
    # 只把「摘要 + 游标之后的原文」发给模型；界面历史一条不删
    msgs = _build_llm_msgs(hist, sys_prompt, turn_sid)
    hist_msgs = len(msgs)
    streaming = False  # 是否已开始向用户流式输出正文
    # 本轮正文累积：流式回调里按节流频率落盘，进程被杀最多丢一小段
    stream_text: list[str] = []
    # 上一轮被用户终止时做过、没做完的工具（见 _aborted_note）：终止就是「别干了」，
    # 不把这段交接说清楚，模型看到自己的话成了半截，下一句就会自作主张接着做
    aborted_note = _aborted_note(hist)
    if aborted_note:
        msgs.append({"role": "system", "content": aborted_note})
    # 本轮真正执行过的工具，终止时随回答一起落盘，供下一轮做交接
    tool_log: list[dict] = [] if tool_log is None else tool_log
    # 上下文里一共有几张画面（含本轮提问自带的那张）。模型自己截图时，新注入的
    # 那张要接着编号，否则它和旧图的说明一字不差，模型分不清哪张是当前的
    turn_shot_no = sum(1 for m in hist if isinstance(m.get("content"), list))

    def _stamp_parts(tail: str = "") -> list[dict]:
        """当前「屏幕上出现过的内容」快照，按原顺序

        events 本身就是有序流（思维链 / 工具块，以及工具调用前已经显示过的
        过渡正文段——那些以 {"type":"text"} 的形式插在 events 里）。
        尾部再补上当刻正在生成的那段正文，就是完整的屏幕内容。
        """
        parts: list[dict] = []
        for ev in events:
            if ev.get("type") == "text":
                parts.append({"type": "text", "content": ev.get("text") or ""})
            else:
                parts.append({"type": "event", "event": ev})
        if tail:
            parts.append({"type": "text", "content": tail})
        return parts

    async def on_delta(text: str):
        nonlocal streaming
        stream_text.append(text)
        if not streaming:
            streaming = True
            # 模型开始写正式回答 = 不再操作桌面。若刚才那一步动屏的遮罩还亮着
            # （正常情况工具收尾就收掉了），这里再兜一次，把屏幕还给用户
            set_overlay(False)
            await manager.broadcast({"type": "stream_start", "ts": time.time(), **_turn_tag()})
        # 增量落盘（此函数是 async 的，前端不会感知到这点磁盘开销）
        try:
            persist_stream("".join(stream_text),
                           parts=_stamp_parts("".join(stream_text)), sid=turn_sid)
        except Exception:
            pass
        # final=False：这段正文可能只是工具调用前的过渡话，前端不写进 crash 缓冲；
        # 本轮结束时会有一次带 final=True 的完整正文推送（内容相同）
        await manager.broadcast({"type": "delta", "text": text, "final": False, **_turn_tag()})

    thought_open = False

    async def on_thought(text: str):
        nonlocal thought_open
        if not thought_open:
            thought_open = True
            await manager.broadcast({"type": "thought_start", "ts": time.time(), **_turn_tag()})
        await manager.broadcast({"type": "thought_delta", "text": text, **_turn_tag()})

    def _output_budget(used_msgs: list[dict]) -> int:
        """按当前上下文剩余空间算出本次调用的输出上限。

        旧逻辑固定 4096，长答复/长工具参数会在中途被硬截断，正文漏出半截标记。
        现在：输出额度 = min(配置上限, 上下文窗口 - 已用 - 预留)，且不低于 4096。
        """
        win = context_window()
        used = _estimate_tokens(used_msgs)
        # 预留一小块给下一轮的用户消息 / 工具结果
        room = max(win - used - 2048, 4096)
        cap = MAX_OUTPUT_TOKENS if MAX_OUTPUT_TOKENS > 0 else win
        return max(min(cap, room), 4096)

    try:
        round_no = 0
        # 本轮是否已经往对话里塞过一张截图（塞过就不再塞第二张）
        turn_shot_seen = False
        # 本轮提问自带画面时，模型再自己截一张就是多余的：它拍到的往往是
        # DeckMeido 面板本身，或者和用户发的那张不是同一帧，多一张图只会让
        # 它挑错图回答。这时把截图工具从这一轮的清单里摘掉
        # 这里的 image_part 是本轮的 [SHOT:文件名] 标记（见 handle_chat），
        # 只用来判断「这一轮带没带画面」，内容本身已经在 msgs 里展开成图片了
        has_shot = image_part is not None
        # 「带图就问一句、不许自己再截」只约束插件端（QAM 截屏询问）：它挂在
        # gamescope 上，模型在那里反复截图会把游戏画面一顿乱拍。桌面/网页端
        # 是照着屏幕干活的，截图是它的眼睛，不能因为带了张图就不许它看。
        # 这里用 from_client 而不是 client：下面工具循环里的 for call 会用到
        # client 这个名字（handle_chat 里本来就是这么命名的），别被它盖掉
        if _shot_lock is None:
            _shot_lock = (from_client or "") == "qam"
        locked_shot = has_shot and _shot_lock
        round_tools = tools.TOOLS if not locked_shot else [
            t for t in tools.TOOLS if t["function"]["name"] != "screenshot"]
        # 带图提问是「一问一答」：模型答完通常会顺手再截一张确认，于是又对着
        # 新画面续一轮，用户看到的是一条提问连着好几张图和好几段回答。
        # 所以带图这轮答完正文就直接收工，不再继续跑工具轮
        if locked_shot:
            extra_system = (extra_system + "\n" if extra_system else "") + (
                "本轮是带着用户给的画面提问的：直接根据这张画面回答即可。"
                "画面就这一张，不要再截图，答完即止。")
        while MAX_TOOL_ROUNDS <= 0 or round_no < MAX_TOOL_ROUNDS:
            round_no += 1
            # 单轮里工具往返也会堆积上下文，超过阈值就先压缩再继续。
            # 压缩只推进游标、不删 history，所以这里重建 msgs 时要保留
            # 本轮新加的工具往返消息（hist_msgs 之后的部分），否则工具链断掉。
            if _need_compact(_estimate_tokens(msgs)):
                # 压缩失败（网络抖动、摘要模型报错）不该把整轮对话弄崩：
                # 压缩只是省上下文，压不了就带着原上下文继续跑
                try:
                    await compact_history(auto=True, hist=hist, sid=turn_sid)
                    rebuilt = _build_llm_msgs(hist, sys_prompt, turn_sid)
                    extra = [m for m in msgs[hist_msgs:]
                             if m.get("role") in ("tool", "assistant")]
                    msgs[:] = rebuilt + extra
                    hist_msgs = len(rebuilt)
                    streaming = False
                    # 压缩前已经显示的过渡正文：作为 text 事件留在 events 里继续保存
                    if "".join(stream_text).strip():
                        events.append({"type": "text", "text": "".join(stream_text)})
                    stream_text.clear()
                except Exception as e:
                    print(f"[DeckMeido] 工具轮中压缩失败，继续：{e}", file=sys.stderr)

            msg = await llm.chat_with_tools_stream(msgs, round_tools, on_delta=on_delta,
                                                   on_thought=on_thought,
                                                   max_tokens=_output_budget(msgs))
            if thought_open:
                await manager.broadcast({"type": "thought_end", "ts": time.time(), **_turn_tag()})
                thought_open = False

            # 撞到输出上限时自动续写，把后半段接上（不再把用户丢给「请拆小步骤」）
            cont = 0
            while (msg.get("finish_reason") == "length" and not (msg.get("tool_calls") or [])
                   and cont < MAX_CONTINUATIONS):
                cont += 1
                partial = (msg.get("content") or "")
                if not partial.strip():
                    break
                msgs.append({"role": "assistant", "content": partial})
                msgs.append({"role": "user", "content":
                             "（回复被输出长度上限截断了，请从断点处直接继续，不要重复已写内容，"
                             "也不要重新开头。）"})
                cont_msg = await llm.chat_with_tools_stream(
                    msgs, [], on_delta=on_delta,
                    max_tokens=_output_budget(msgs))
                more = (cont_msg.get("content") or "")
                if cont_msg.get("reasoning_content"):
                    events.append({"type": "thought", "text": cont_msg["reasoning_content"]})
                msg = {"role": "assistant", "content": partial + more,
                       "finish_reason": cont_msg.get("finish_reason")}
                if cont_msg.get("usage"):
                    msg["usage"] = cont_msg["usage"]
                # 续写时模型改了主意想调工具：停止续写，按无工具收尾处理
                if cont_msg.get("tool_calls"):
                    break
                if not more.strip():
                    break

            # 记录本轮真实占用：优先服务商的 prompt_tokens，拿不到就按字符估算
            _ctx_used = _split_usage(msg.get("usage")) or _estimate_tokens(msgs)
            await manager.broadcast({"type": "status", "data": get_status()})

            # 思维链（reasoning_content，DeepSeek/Qwen 等支持）
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                events.append({"type": "thought", "text": reasoning})

            tool_calls = msg.get("tool_calls") or []
            # 每轮模型返回都记一笔：这一步到底动没动手、动了什么。
            # 「AI 说按了但游戏没反应」时，这条能立刻区分是模型没调工具
            # （纯嘴上说），还是调了但注入没生效
            print(f"[DeckMeido] 模型第 {round_no} 轮：工具 {len(tool_calls)} 个"
                  + (f"（{'、'.join(c['function']['name'] for c in tool_calls)}）"
                     if tool_calls else "，没有动手，直接作答")
                  + f"，正文 {len(msg.get('content') or '')} 字")
            if not tool_calls:
                # 续写后仍被截断：内容已经流式显示给用户了，别丢，末尾加一句说明
                if msg.get("finish_reason") == "length":
                    body = (msg.get("content") or "").strip()
                    note = ("\n\n（以上内容到了输出上限，续写也没能收尾；"
                            "要我接着写就说「继续」。）")
                    return (body + note) if body else note.strip()
                print("[DeckMeido] 本轮收尾：模型没有再调用工具")
                return msg.get("content") or "（没有回复内容）"

            # 带图这轮只答一次：模型拿到工具结果后常常再截一张确认，
            # 于是又对着新画面续一轮，用户看到一条提问连着好几张图和好几段回答。
            # 所以带上图提问时，答完第一段正文就收工
            if locked_shot and "".join(stream_text).strip():
                return "".join(stream_text).strip()

            # 动屏幕的工具一律不给：插件端带图这轮只要「看图说怎么办」，动手会换掉
            # 用户截图时的画面，答的内容也就对不上他看到的那一帧了
            if locked_shot:
                # touches_screen 要的是解析后的参数（要判断 desktop 的 action、
                # run_command 的命令内容），而 arguments 这里是 JSON 字符串
                def _screen_call(c: dict) -> bool:
                    try:
                        a = json.loads(c["function"].get("arguments") or "{}")
                    except (ValueError, TypeError):
                        a = {}
                    return tools.touches_screen(c["function"]["name"], a if isinstance(a, dict) else {})

                dropped = [t["function"]["name"] for t in tool_calls if _screen_call(t)]
                if len(dropped) == len(tool_calls):
                    # 整批都是屏幕操作：连同模型那句「我先截个图」一起作废，
                    # 让它直接按已有画面作答，界面上不留一次空工具调用
                    stream_text.clear()
                    msgs.append({"role": "assistant", "content": msg.get("content") or ""})
                    msgs.append({"role": "user", "content":
                                 "（不用截图也不用操作屏幕，直接根据上面那张画面回答。）"})
                    continue
                tool_calls = [c for c in tool_calls if not _screen_call(c)]

            # 有工具调用：本轮流式输出的内容是中间话，最终答复会重新流式。
            # 但用户屏幕上已经看到了，必须以 text 事件留在 events 里，
            # 否则中途进程被杀，快照里只剩最后一段 → 「只看到一点点内容」
            if "".join(stream_text).strip():
                events.append({"type": "text", "text": "".join(stream_text)})
            streaming = False
            stream_text.clear()
            msgs.append(msg)
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                # ask_user：提问 / 选择题。不执行任何东西，直接把问题抛给界面，
                # 这一轮就此收工；用户点的那一项作为下一条普通消息发回来，
                # 于是它天然走完一轮「回答→再提问」的循环，不用另立挂起状态
                if name == "ask_user":
                    q = str(args.get("question") or "").strip()
                    opts = [str(o).strip() for o in (args.get("options") or [])
                            if str(o).strip()][:6]
                    ask = {"question": q, "options": opts}
                    events.append({"type": "ask", **ask})
                    await manager.broadcast({"type": "ask", "ts": time.time(),
                                             **ask, **_turn_tag()})
                    return q or "（想跟你确认一件事，但问题没写出来）"

                # 完全访问：用户已预先授权一切操作，连 gamepad / install_game 这些
                # 往常必须点头的也不拦。只有升级例外 —— 它会让服务重启、对话中断，
                # 属于「做完就跟当前这轮没关系了」的操作，无论什么模式都要确认
                needs_approval = False if PERMISSION_MODE == "full" else name in tools.DANGEROUS
                # 升级会让服务重启、对话中断，无论什么权限模式都要用户点头
                if name == "self_update" and args.get("action") == "upgrade":
                    needs_approval = True
                if name == "run_command":
                    cmd = str(args.get("command", ""))
                    if PERMISSION_MODE == "manual":
                        needs_approval = True
                    elif PERMISSION_MODE == "auto":
                        needs_approval = tools.is_dangerous(cmd)
                    # full 模式下不拦截

                if needs_approval:
                    # 挂起危险操作，等用户下一轮确认。把「谁在哪提的」一并记下，
                    # 确认时校验（见 _pending_matches），别的端说的「好」不算数
                    pending_action = {"name": name, "args": args,
                                      "description": tools.describe(name, args),
                                      "sid": store.turn.get("sid") or store.current_sid,
                                      "epoch": store.turn.get("epoch") or store.epoch,
                                      "turn_id": store.turn.get("turn_id") or "",
                                      "client": store.turn.get("client") or "",
                                      "device": store.turn.get("device") or ""}
                    print(f"[DeckMeido] 工具待确认 {name}（模式 {PERMISSION_MODE}）："
                          f"{pending_action['description']}")
                    return f"我需要执行一个会影响设备的操作：\n\n**{pending_action['description']}**\n\n回复「确认」我就执行，说别的就当作取消。"

                # 文件改动识别（run_command）
                files = tools.extract_writes(str(args.get("command", ""))) if name == "run_command" else []
                label = tools.cn_name(name)
                summary = tools.summarize_tool(name, args)
                event = {"type": "tool", "name": name, "label": label,
                         "summary": summary, "files": files}
                # 路径一律给全：AI 给的常常是相对工作区的路径，面板直接拿去
                # 跳目录会落到后端进程的 cwd 上（一般是家目录），跟 AI 看的地方对不上
                if name in ("read_file", "write_file", "edit_file", "list_dir"):
                    raw_path = str(args.get("path", ""))
                    if raw_path:
                        # 相对路径按工作区解释（和文件工具一致），不按后端 cwd
                        p = Path(raw_path).expanduser()
                        event["path"] = str(p if p.is_absolute() else tools.WORKSPACE / p)
                activity = f"正在{label}：{summary}" if summary else f"正在{label}"
                await manager.broadcast({"type": "tool_activity", "text": activity, "ts": time.time(), **_turn_tag()})
                # 终端联动：必须在执行之前推「开跑」，面板据此立刻挂一条执行中的记录
                # 并开始轮询实时输出。放到执行后面就只剩「已结束」一条，
                # 执行中的动画和进度全看不见了
                if name == "run_command" and str(args.get("command") or "").strip():
                    claim = tools.CURRENT_CMD
                    await manager.broadcast({"type": "term_start",
                                             "command": str(args.get("command")),
                                             "reason": str(args.get("reason") or ""),
                                             "cwd": (claim or {}).get("cwd", ""),
                                             "track": bool(claim and claim.get("command") == args["command"]),
                                             # 面板记下推送时刻，用来对齐轮询拿到的 started：
                                             # 对不上说明这条命令已经跑完、CURRENT_CMD 换了，
                                             # 别把它当成还在执行的那条一直转圈
                                             "ts_sent": time.time(),
                                             "ts": time.time(), **_turn_tag()})
                # 文件浏览器联动：凡是带路径的工具都推一条，面板据此自动跳目录。
                # 只推路径不推内容 —— 面板自己再走 /api/browse 取，权限和越界检查
                # 都留在那一个接口里
                if event.get("path"):
                    await manager.broadcast({"type": "fs_follow", "path": event["path"],
                                             "is_dir": name == "list_dir",
                                             "ts": time.time(), **_turn_tag()})
                # 遮罩只在真的要动屏幕的这一步亮起（见上面 handle_chat 里的说明）。
                # 放在真正执行前才 set_overlay(True)：鼠标注入期间 tools 会让遮罩
                # 穿透（set_overlay_passthrough），否则那一下点击会点在遮罩上
                # 被当成「用户终止」。
                # pointer：这一步会不会按下鼠标键（点击/滚动/拖拽/滑动）。之前在 tools
                # 内部自己开穿透，而穿透在遮罩还没显示时什么都不做，于是遮罩随后
                # 才冒出来，正好接住那一下点击 —— 这里必须先让遮罩就位再看这个标志
                on_screen = tools.touches_screen(name, args)
                pointer = on_screen and tools.pointer_action(name, args)
                set_overlay_pointer(pointer)
                if on_screen:
                    # 拉起遮罩最长要等 6 秒旧实例退位，必须走线程，否则整个 ws 卡住
                    await set_overlay_async(True, activity)
                # 先记一条「没执行完」的：被终止时 CancelledError 会从这里穿过去，
                # 于是交接说明里能准确指出「这一步断了」。正常跑完再把它改成已完成
                log_item = {"name": name, "summary": summary, "interrupted": True}
                tool_log.append(log_item)
                try:
                    result = await tools.execute(name, args)
                finally:
                    if on_screen:
                        set_overlay(False)
                    set_overlay_pointer(False)
                log_item["interrupted"] = False
                # 终端输出截断后随事件展示（网页端显示为终端块）
                event["output"] = result[:1500] + ("\n…（截断）" if len(result) > 1500 else "")
                if name in ("write_file", "edit_file") and tools.LAST_DIFF:
                    event["diff"] = tools.LAST_DIFF
                if name == "screenshot":
                    m = re.search(r"/shots/\S+?\.png", result)
                    if m:
                        event["image"] = m.group(0)
                    else:
                        # 抓帧失败：接口层要把原因带到前端，不能让用户只看到
                        # 一行「已截屏」却一直没有画面
                        event["error"] = shot_state.get("error") or "未知原因"
                events.append(event)
                await manager.broadcast({"type": "tool_done", "event": event, "ts": time.time(), **_turn_tag()})
                # 工具结果前面统一加「[工具名] 」，这类元信息在请求最前面：
                # 同一次调用无论正文多长，前缀都一样，DeepSeek 的前缀缓存才命中得上。
                # 反过来，若这里塞「本次命中缓存」之类的可变文案，后面整段历史
                # 立刻全部失配，一次工具往返就能把几万 token 从命中打回未命中
                msgs.append({"role": "tool", "tool_call_id": call["id"],
                             "content": f"[{name}] {result}"})
                # 截图后把画面注入对话，多模态模型可直接看图回答。
                # 同一轮里连着截第二张时复用上一张：模型反复截图通常不是画面变了，
                # 而是它没「看到」上面那张。再截一张只会让上下文里堆图，
                # 顺手还把模型推回同样的困惑里绕圈（曾经连截五次停不下来）。
                if name == "screenshot" and shot_state.get("vision"):
                    if turn_shot_seen:
                        msgs.append({"role": "user", "content":
                                     "（本轮已经截过图，画面就在上面，直接看图回答，不要重复截图）"})
                    else:
                        turn_shot_seen = True
                        turn_shot_no += 1
                        try:
                            import base64
                            b64 = base64.b64encode(Path(shot_state["vision"]).read_bytes()).decode()
                            msgs.append({"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                {"type": "text", "text": _shot_caption(max(turn_shot_no, 1))},
                            ]})
                        except Exception:
                            pass
        # 工具轮数用尽：最后再不给工具地问一次，让它基于已有结果收尾回答
        msg = await llm.chat_with_tools_stream(msgs, [], on_delta, max_tokens=_output_budget(msgs))
        if msg.get("reasoning_content"):
            events.append({"type": "thought", "text": msg["reasoning_content"]})
        answer = (msg.get("content") or "").strip()
        limit_note = (f"（已经连续执行 {MAX_TOOL_ROUNDS} 步，到了单轮上限，后面的步骤没有继续。"
                      f"要接着做的话说一声，或把 max_tool_rounds 调大）")
        # 收尾时模型经常什么都不写：这不是正常答复，要把「为什么停」说清楚
        return f"{answer}\n\n{limit_note}" if answer else limit_note
    except Exception as e:
        # 把失败当正常回复返回，下一轮模型会在自己的历史里读到一句「LLM 调用失败」，
        # 于是把这句话当成它说过的话继续往下聊。明确标成中断，交给上层提示
        print(f"[DeckMeido] LLM 调用失败：{e}", file=sys.stderr)
        raise RuntimeError(f"LLM 调用失败：{e}") from e


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(default="")):
    if not _norm_token(TOKEN) or _norm_token(token) != _norm_token(TOKEN):
        # 先 accept 再 close，浏览器才能拿到 4401 这个原因码，否则只会看到 1006 而无限重连
        await ws.accept()
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    # 来源：优先信调用方显式声明的 client（插件标 qam，桌面客户端标 client），
    # 没声明就按 UA 猜——手机浏览器自动算 mobile，其余算 web。
    # ws_mode 用来记住「这条连接是在什么模式下连上的」：状态广播是全局的，
    # 游戏模式的判定（PipeWire 里的 gamescope 节点）对所有连着的端都成立，桌面网页就会
    # 跟着显示成「游戏模式」。所以按连接自己的会话判定，桌面网页永远是桌面
    ua = ws.headers.get("user-agent", "")
    ua_client = "mobile" if ("Mobile" in ua or "Android" in ua) else "web"
    ws_mode = (await asyncio.to_thread(get_status))["mode"]
    try:
        # 新连接同步历史（带上 sid，前端才能建立会话代次基线，
        # 否则 currentSid 一直是空值，代次过滤整条失效）
        await ws.send_json({"type": "history", "messages": store.history,
                            "sid": store.current_sid, "my_client": ua_client})
        await ws.send_json({"type": "status",
                            "data": {**get_status(), "mode": ws_mode}})
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                # 前端心跳：回一条 pong 让它知道链路还活着
                await ws.send_json({"type": "pong"})
                continue
            if data.get("type") == "history":
                # 客户端断线重连后主动要一份：握手时后端已经发过一次，
                # 但断线期间别的端说的话它没收到，得重新对齐
                await ws.send_json({"type": "history", "messages": store.history,
                                    "sid": store.current_sid, "my_client": ua_client})
                continue
            if data.get("type") == "chat":
                # 开始发问就刷新：这条连接此刻在什么模式下，就是它自己的模式
                ws_mode = (await asyncio.to_thread(get_status))["mode"]
                try:
                    # 插件那条路要从 ws 直接带图和真实工作区进来：
                    # 它挂在 gamescope 上，请求和流式都在同一条连接里，
                    # 走不了"先 POST 拿结果再订阅"的两段式。
                    ws_root = data.get("workspace")
                    await start_chat(
                        str(data.get("text", "")).strip(),
                        image=data.get("image") or None,
                        image_prompt=data.get("image_prompt")
                        or "（这是当前游戏画面，结合画面回答用户的问题）",
                        workspace=Path(ws_root).expanduser() if ws_root else None,
                        client=str(data.get("client") or ua_client),
                        device=str(data.get("device") or "").strip() or None,
                        brief=str(data.get("brief") or "").strip(),
                        turn_id=str(data.get("turn_id") or ""),
                    )
                except asyncio.CancelledError:
                    pass  # 用户点了终止，保持 ws 连接
                except Exception as exc:
                    # 一轮对话内部报错不能把整条 ws 打死：旧逻辑异常直接冒到外层，
                    # 连接被断 → 前端 busy 卡死 → 后续消息全排队发不出去
                    try:
                        await ws.send_json({
                            "type": "assistant_message",
                            "text": f"这轮出错了：{exc}",
                            "ts": time.time(), "events": [],
                        })
                    except Exception:
                        pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 兜底：不管怎么退出都要摘掉连接，否则 broadcast 会一直往死连接上发
        await manager.disconnect(ws)


@app.get("/shots/local", dependencies=[Depends(check_token)])
async def shots_local(path: str = Query(default="")):
    """把插件截图落在本机 /tmp 下的那份读出来给各端显示

    插件的图在交给后端之前只存在于 Deck 的临时目录：电脑网页那头刷新历史时，
    后端这边还没落盘，屏幕上就只有一条没有画面的提问。所以允许按绝对路径取。
    只认 /tmp 下、JPEG 结尾的文件，别的一概不认——否则这个接口等于开放整台
    Deck 的文件读取。
    """
    p = Path(path)
    try:
        real = p.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if (real.suffix.lower() not in (".jpg", ".jpeg")
            or not str(real).startswith("/tmp/") or not real.is_file()):
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(real, media_type="image/jpeg")


@app.get("/api/local_shot", dependencies=[Depends(check_token)])
async def local_shot(name: str = Query(default="")):
    """按文件名回读一张随提问落盘的画面

    图在历史里只留一条 [SHOT:文件名] 标记，发给模型时才按文件读成 base64
    （见 _shot_ref_content）。所以这个文件不是「纯展示副本」：删了，模型也
    看不到那张画面了，界面上则是破图。因此除了 /shots/ 静态目录，再留这条
    按文件名取的路径，各端都按文件名取图，和 /shots/ 是同一份文件。
    name 只接受纯文件名（相对 shots 目录），不允许带目录分隔符，防目录穿越。
    库里没有这张文件时回 404，前端据此显示占位而不是一直转圈。
    """
    fname = Path(name).name
    if not name or fname != name:
        raise HTTPException(status_code=404, detail="file not found")
    shots_dir = Path(__file__).resolve().parent / "static" / "shots"
    target = shots_dir / fname
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target)


# 静态网页控制台（放最后挂载，避免覆盖 API 路由）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
