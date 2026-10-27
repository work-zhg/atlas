"""模型网关（doc/model-design.html §6.7）在知识库这一侧：按平台用途调模型。

    kb_enrich      入库增强（上下文补全、表格摘要）
    kb_query       查询理解（改写、拆分）
    kb_embedding   远程嵌入：每个库建库时从允许范围里选定，之后不变
    kb_rerank      远程重排

★ 调用方只说「我是哪个应用、做什么用」：请求头 X-Model-Purpose + X-Caller-App，不带 Key，
  也不持有供应商 Key。对话的模型名按用途的默认模型取（短缓存）；嵌入、重排的模型名由库自己记着，
  网关只放行用途允许范围里的。
★ 改用途的默认模型只影响对话类用途：已有库的向量模型和索引绑在一起，不随它变（§6.7）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .llm import OpenAICompatibleChat

__all__ = ["PurposeCatalog", "PurposeChat", "purpose_headers"]

logger = logging.getLogger(__name__)


def purpose_headers(purpose: str, app_code: str) -> dict[str, str]:
    return {"X-Model-Purpose": purpose, "X-Caller-App": app_code}


class PurposeCatalog:
    """网关的平台用途配置（/internal/catalog）：TTL 内直接用；网关不可用时继续用上一份。"""

    def __init__(
        self,
        base_url: str,
        *,
        ttl_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._ttl = ttl_s
        self._transport = transport
        self._value: dict[str, dict[str, Any]] | None = None
        self._at = 0.0

    async def purposes(self) -> dict[str, dict[str, Any]]:
        if self._value is not None and time.monotonic() - self._at < self._ttl:
            return self._value
        try:
            async with httpx.AsyncClient(
                timeout=10.0, trust_env=False, transport=self._transport
            ) as http:
                r = await http.get(f"{self.base_url}/internal/catalog")
                r.raise_for_status()
                self._value = {str(p["code"]): dict(p) for p in r.json().get("purposes") or ()}
        except httpx.HTTPError as exc:
            if self._value is None:
                raise
            logger.warning("模型网关不可用，继续用缓存的平台用途配置：%s", exc)
        self._at = time.monotonic()
        return self._value

    async def default(self, purpose: str) -> str | None:
        value = ((await self.purposes()).get(purpose) or {}).get("default_model")
        return str(value) if value else None

    async def options(self, purpose: str) -> list[str]:
        """用途允许的模型（默认的在前）：建库时可选的远程嵌入、重排模型。"""
        p = (await self.purposes()).get(purpose) or {}
        names = [p.get("default_model"), *(p.get("allowed_models") or [])]
        return [str(n) for n in dict.fromkeys(names) if n]


class PurposeChat:
    """ChatModel：按用途调模型网关的 OpenAI 兼容对话接口，模型名按用途配置取。

    ★ 用途还没配模型时 complete() 抛 RuntimeError：调用方按「LLM 不可用」降级（同没配 LLM）。
    """

    def __init__(
        self,
        base_url: str,
        purpose: str,
        catalog: PurposeCatalog,
        *,
        app_code: str,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.purpose = purpose
        self.model = ""
        self._catalog = catalog
        self._chat = OpenAICompatibleChat(
            f"{base_url.rstrip('/')}/v1",
            "",
            "",
            timeout_s=timeout_s,
            transport=transport,
            headers=purpose_headers(purpose, app_code),
        )

    async def aclose(self) -> None:
        await self._chat.aclose()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str:
        name = await self._catalog.default(self.purpose)
        if not name:
            raise RuntimeError(f"平台用途 {self.purpose} 还没有配置模型")
        self.model = self._chat.model = name
        return await self._chat.complete(
            messages, max_tokens=max_tokens, temperature=temperature, json_mode=json_mode
        )
