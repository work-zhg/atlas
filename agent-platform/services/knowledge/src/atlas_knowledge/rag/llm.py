"""知识库用到的 LLM（doc/knowledge-enterprise-design.html §5.3 / §5.5 / §7.1）。

上下文补全、表格摘要、查询理解都只要一次「消息进、文本出」的调用；走 OpenAI 兼容的
chat/completions（模型网关、DeepSeek、vLLM 都兼容）。

★ 没配 LLM 时 Runtime.llm 为 None：所有依赖 LLM 的增强都降级为不做，入库与检索不受影响。
★ 失败自己兜：429 / 5xx / 超时按指数退避重试，仍失败就抛 —— 由调用方决定降级。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

import httpx

__all__ = ["ChatModel", "OpenAICompatibleChat", "parse_json"]

logger = logging.getLogger(__name__)

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ChatModel(Protocol):
    model: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str: ...


class OpenAICompatibleChat:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self._retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # headers：经模型网关时的平台用途（X-Model-Purpose、X-Caller-App），不带 Key
            headers={
                **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
                **(headers or {}),
            },
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        delay = 1.0
        for attempt in range(self._retries + 1):
            try:
                r = await self._client.post("/chat/completions", json=body)
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
            else:
                if r.status_code < 400:
                    data = r.json()
                    return str(data["choices"][0]["message"].get("content") or "")
                if r.status_code not in _RETRY_STATUS or attempt == self._retries:
                    r.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")


def parse_json(text: str) -> Any:
    """模型输出 → JSON。容忍 ```json 代码块包裹与前后的说明文字。"""
    text = text.strip()
    if m := re.search(r"```(?:json)?\s*(.*?)```", text, re.S):
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise
        return json.loads(text[start : max(text.rfind("}"), text.rfind("]")) + 1])
