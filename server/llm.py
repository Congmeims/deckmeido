"""OpenAI 兼容 LLM 客户端"""
from __future__ import annotations

import httpx


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

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

    async def close(self):
        await self._client.aclose()
