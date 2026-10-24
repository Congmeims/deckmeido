"""OpenAI 兼容 LLM 客户端"""
from __future__ import annotations

import json
import re

import httpx

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
                 max_tokens: int = 4096):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        # 不传 max_tokens 时服务端默认值往往偏小，流式输出会在工具调用中途被硬截断，
        # 导致半截标记漏进正文、整轮操作中断
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

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
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

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
                                     on_delta=None, on_thought=None) -> dict:
        """流式调用：正文增量经 on_delta、思维链增量经 on_thought 回调，最终返回聚合后的完整 message"""
        payload = {"model": self.model, "messages": messages,
                   "tools": tools, "tool_choice": "auto", "stream": True,
                   "max_tokens": self.max_tokens}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        finish_reason = None
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                fr = (chunk.get("choices") or [{}])[0].get("finish_reason")
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
        return msg

    async def close(self):
        await self._client.aclose()
