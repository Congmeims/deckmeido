"""OpenAI 兼容 LLM 客户端"""
from __future__ import annotations

import json
import re
import time

import httpx

# 图片按 base64 内联进请求体，一张图就能到千万字符级。这里在发车前量一次
# 请求体大小并记下来：出问题时能分清「体积大」和「真的花掉这么多 token」，
# 只看字符数会误以为是模型在按字符计费
_LAST_BODY = 0

# 最近一次请求的前缀缓存命中情况，供状态接口展示（0/0 = 服务商不返回）
_LAST_CACHE = {"hit": 0, "prompt": 0, "at": 0.0}


def _body_size(payload: dict) -> int:
    """算出这次请求体的字符数（不序列化到网络，只做量尺）"""
    global _LAST_BODY
    try:
        _LAST_BODY = len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        _LAST_BODY = 0
    return _LAST_BODY


def _log_usage(usage: dict | None) -> None:
    """把服务商回的真实用量打到日志：prompt_tokens 才是这一轮烧掉的输入

    DeepSeek 会把输入拆成「命中缓存」和「未命中」两部分：prompt_tokens 是两者之和，
    prompt_cache_hit_tokens 才是走了前缀缓存的那段。命中部分按更低的单价计费，
    所以日志里同时给出命中率 —— 这个数字掉下来，就说明前缀被别人改动了（见 main.py 里
    工具结果前缀、历史裁剪那几处），是排查「为什么突然变贵」的第一手线索。
    """
    if not isinstance(usage, dict):
        return
    pin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    pout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens")
              or usage.get("prompt_tokens_details", {}).get("cached_tokens")
              or usage.get("cached_tokens") or 0)
    if hit:
        rate = (hit / pin * 100) if pin else 0.0
        print(f"[DeckMeido] LLM 用量：请求体 {_LAST_BODY} 字符 → "
              f"输入 {pin} token（缓存命中 {hit}，{rate:.0f}%），输出 {pout} token",
              flush=True)
        _LAST_CACHE.update(hit=hit, prompt=pin, at=time.time())
        return
    _LAST_CACHE.update(hit=0, prompt=pin, at=time.time())
    print(f"[DeckMeido] LLM 用量：请求体 {_LAST_BODY} 字符 → "
          f"输入 {pin} token，输出 {pout} token", flush=True)


# 部分模型（DeepSeek 系）会把工具调用写成正文里的 DSML 标记而不是标准 tool_calls，
# 这类片段绝不能展示给用户。标记一旦出现，后面全是工具调用内容或残缺标签，直接截断。
_START_RE = re.compile(r"<[｜|]{1,2}\s*DSML|<[｜|]{1,2}\s*(?:tool_)?calls?[｜|>\s]",
                       re.IGNORECASE)


def strip_tool_markup(text: str) -> str:
    """去掉正文里泄漏的工具调用标记（含被截断的半截标签）"""
    if not text:
        return text
    m = _START_RE.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0,
                 max_tokens: int = 4096, reasoning_effort: str = "high"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        # 不传 max_tokens 时服务端默认值往往偏小，流式输出会在工具调用中途被硬截断，
        # 导致半截标记漏进正文、整轮操作中断
        self.max_tokens = max_tokens
        # 实测 deepseek-v4.1-flash 等服务商默认不产出 reasoning_content，
        # 必须显式传 reasoning_effort 才会返回思维链
        self.reasoning_effort = reasoning_effort
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def is_deepseek(self) -> bool:
        """是不是 DeepSeek 官方接口（决定要不要发它的专属参数）"""
        return "deepseek" in self.base_url.lower()

    def normalize(self) -> None:
        """把「照抄别人家的写法」纠正成 DeepSeek 认的写法

        三处最常见的手填错误：地址带 /v1（那是兼容别名，官方域名本身就已经是
        .../chat/completions 的根）、模型名填了 OpenAI 的 gpt-*、模型名填了要
        单开的思考模型（deepseek-reasoner 不支持工具调用，本插件全靠工具干活）。
        前两种是直接 400 或「模型不存在」，第三种要等第一句才失败，所以一起归一。
        """
        if not self.is_deepseek:
            return
        url = self.base_url.rstrip("/")
        if url.lower().endswith("/v1"):
            self.base_url = url[:-3].rstrip("/")
        m = (self.model or "").strip().lower()
        if (not m or m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3")
                or "reasoner" in m):
            self.model = "deepseek-chat"
        # DeepSeek 只认 high / max，low 与 medium 会被它映射成 high；空串表示
        # 不带参数（即不思考）。归一之后前后端看到的是同一个值，滑条才不会跳档
        if self.reasoning_effort not in ("", "high", "max"):
            self.reasoning_effort = "high"

    def cache_stats(self) -> dict:
        """最近一次请求的缓存命中：{hit, prompt, percent, idle}；服务商不返回时为 0

        idle 是距上次请求过去的秒数：DeepSeek 的硬盘缓存闲置几小时后会自动清掉，
        前端拿这个数提示「命中率突然变低可能只是放太久，不一定是配置坏了」。

        作为实例方法而不是模块函数：主进程里的客户端才是真正发请求的那一个，
        前端读到的数字必须来自同一份状态。
        """
        hit, prompt = _LAST_CACHE["hit"], _LAST_CACHE["prompt"]
        return {"hit": hit, "prompt": prompt,
                "percent": round(hit / prompt * 100, 1) if prompt else 0.0,
                "idle": round(time.time() - _LAST_CACHE["at"]) if _LAST_CACHE["at"] else 0}

    async def list_models(self) -> list[str]:
        """拉取服务商的模型列表（OpenAI 兼容的 GET /models）"""
        resp = await self._client.get(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        names = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else str(it)
            if mid:
                names.append(mid)
        return sorted(set(names))

    async def chat(self, messages: list[dict]) -> str:
        # 摘要/收尾这类一次性调用也走流式：DeepSeek 每次非流式请求都会把整段
        # 输出算完才返回，长摘要会卡到超时；流式还能顺带拿到 usage（缓存命中统计）
        msg = await self.chat_with_tools_stream(messages, [], max_tokens=0)
        return msg.get("content") or ""

    async def chat_with_tools(self, messages: list[dict], tools: list[dict]) -> dict:
        """带工具调用的对话，返回完整 message（含 content / tool_calls）"""
        payload = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]

    async def chat_with_tools_stream(self, messages: list[dict], tools: list[dict],
                                     on_delta=None, on_thought=None,
                                     max_tokens: int | None = None) -> dict:
        """流式调用：正文增量经 on_delta、思维链增量经 on_thought 回调，最终返回聚合后的完整 message

        max_tokens 传入时覆盖实例默认值，供上层按当前上下文剩余空间动态放大输出额度；
        传 0 表示不带额度（服务端默认），供摘要这类不在乎长度的内部调用使用。
        """
        budget = self.max_tokens if max_tokens is None else max_tokens
        payload = {"model": self.model, "messages": messages,
                   "stream": True,
                   "stream_options": {"include_usage": True}}
        # 没有工具时不能把 tools 传成空数组：DeepSeek 会回
        # 「tools 参数非法」，整轮续写/收尾直接失败
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if budget and budget > 0:
            # 官方的 chat schema 只有 max_tokens（即使新版模型也是这个字段名），
            # 换成 OpenAI 的 max_completion_tokens 在 DeepSeek 上会直接 400
            payload["max_tokens"] = budget
        if self.reasoning_effort:
            # 两家的开关同名不同义：OpenAI 系（o 系列 / GPT-5）只有 reasoning_effort；
            # DeepSeek 用 thinking 开关（默认就是 enabled），强度也叫 reasoning_effort
            # 但只认 high / max，low、medium 会被它自己映射成 high
            payload["reasoning_effort"] = self.reasoning_effort
        if self.is_deepseek:
            # 固定一个 user_id：DeepSeek 按它做 KVCache 隔离（也用于限速分桶）。
            # 不传时同一账号下不同会话共用缓存池，前缀撞车时命中率会变得不可解释；
            # 传定值则「这台 Deck 的历史」始终落在同一个桶里
            payload["user_id"] = "deckmeido"
        _body_size(payload)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        finish_reason = None
        usage = None
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                # 兼容 "data:" 与 "data: "（DeepSeek/部分网关带空格）两种写法
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # 部分服务商在流末尾单独发一个只含 usage 的分片（需 stream_options）
                if chunk.get("usage"):
                    usage = chunk["usage"]
                # 部分网关把错误塞在 200 的流里（data: {"error": {...}}），
                # 不接住的话会表现成「模型什么都没说」
                if chunk.get("error") and not chunk.get("choices"):
                    err = chunk["error"]
                    raise RuntimeError(str((err or {}).get("message") or err))
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    if on_delta:
                        await on_delta(delta["content"])
                # 不同供应商的思维链字段：reasoning_content / reasoning
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if rc:
                    reasoning_parts.append(rc)
                    if on_thought:
                        await on_thought(rc)
                for tc in delta.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    acc = tool_acc.setdefault(i, {"id": "", "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["function"]["arguments"] += fn["arguments"]
        msg: dict = {"role": "assistant", "content": strip_tool_markup("".join(content_parts))}
        if finish_reason:
            msg["finish_reason"] = finish_reason
        if reasoning_parts:
            msg["reasoning_content"] = "".join(reasoning_parts)
        if tool_acc:
            msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
        if usage:
            msg["usage"] = usage
        _log_usage(usage)
        return msg

    async def close(self):
        await self._client.aclose()
