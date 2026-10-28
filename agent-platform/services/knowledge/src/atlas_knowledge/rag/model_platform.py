"""模型管理平台（agent-platform/doc/design/model-design.html 第 9 章）在知识库这一侧：
服务级客户端 Key。

    用途        客户端 Key（部署配置）   模型
    入库增强    chat_client              enrich_model
    查询理解    chat_client              query_model（空 = enrich_model）
    远程嵌入    embedding_client         每个库建库时从这把 Key 的可用模型里选定
    远程重排    rerank_client            检索设置里从这把 Key 的可用模型里选

★ 管理员事先在平台上建好这几把客户端 Key（如客户端类型 Service）、分配模型；知识库按「类型:编码」
  取 Key，带着它直连 LiteLLM。Key 原文只在内存里，不持有供应商 Key。
★ Key 随时可能被重置：请求时才取（短缓存），LiteLLM 回 401 就重新取一次再试。
★ 平台暂时不可用：沿用上次取到的；从没取到过的，这项能力暂时不可用（入库增强降级为不做）。
★ 取 Key 是同步的：嵌入、重排本来就在线程里调（含 Celery worker）；对话在事件循环里，
  经 asyncio.to_thread 取，平台卡住也不会冻住事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .llm import OpenAICompatibleChat

__all__ = [
    "PlatformChat",
    "PlatformKeyAuth",
    "PlatformUnavailable",
    "ServiceKey",
    "ServiceKeys",
    "parse_client",
]

logger = logging.getLogger(__name__)


class PlatformUnavailable(RuntimeError):
    """连不上模型管理平台，或平台回了错误（而且从没取到过这把 Key）。"""


def parse_client(spec: str | None) -> tuple[str, str] | None:
    """部署配置里的「客户端类型:客户端编码」（如 Service:knowledge-chat）。空 = 没配。"""
    if not spec:
        return None
    client_type, sep, code = spec.partition(":")
    if not sep or not client_type or not code:
        raise ValueError(f"客户端 Key 要写成「类型:编码」：{spec!r}")
    return client_type, code


@dataclass(frozen=True)
class ServiceKey:
    """一把客户端 Key 此刻的样子。status：pending_models / active / blocked / no_models。"""

    status: str
    key: str | None = None
    models: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return self.status == "active" and bool(self.key)


class ServiceKeys:
    """按「类型:编码」取客户端 Key：短缓存；平台不可用时沿用上次取到的。线程安全。"""

    def __init__(
        self,
        platform_url: str,
        *,
        app_code: str,
        ttl_s: float = 60.0,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=platform_url.rstrip("/"),
            timeout=timeout_s,
            trust_env=False,
            transport=transport,
            headers={"X-Caller-App": app_code},
        )
        self._ttl = ttl_s
        self._rows: dict[tuple[str, str], tuple[float, ServiceKey]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def get(self, client: tuple[str, str], *, fresh: bool = False) -> ServiceKey:
        with self._lock:
            row = self._rows.get(client)
        if not fresh and row is not None and time.monotonic() - row[0] < self._ttl:
            return row[1]
        try:
            found = self._fetch(client)
        except PlatformUnavailable:
            if row is None:
                raise
            logger.warning("模型管理平台不可用，沿用上次取到的客户端 Key（%s:%s）", *client)
            return row[1]
        with self._lock:
            self._rows[client] = (time.monotonic(), found)
        return found

    def _fetch(self, client: tuple[str, str]) -> ServiceKey:
        try:
            r = self._http.get(
                "/internal/client-keys",
                params={"client_type": client[0], "client_code": client[1]},
            )
        except httpx.HTTPError as exc:
            raise PlatformUnavailable(f"模型管理平台暂时不可用：{type(exc).__name__}") from exc
        if r.status_code == 404:
            # 平台上没有这把 Key：当作不可用（不是平台故障）
            return ServiceKey(status="missing")
        if r.status_code >= 400:
            raise PlatformUnavailable(f"模型管理平台返回 HTTP {r.status_code}")
        body = r.json()
        return ServiceKey(
            status=str(body.get("status") or ""),
            key=body.get("key"),
            models=tuple(str(m["name"]) for m in body.get("models") or ()),
        )


def _require(found: ServiceKey, client: tuple[str, str]) -> str:
    if not found.usable or found.key is None:
        raise PlatformUnavailable(
            f"客户端 Key {client[0]}:{client[1]} 现在不可用（{found.status}）"
        )
    return found.key


class PlatformKeyAuth(httpx.Auth):
    """同步客户端（远程嵌入、重排）用：每个请求带上最新的 Key；LiteLLM 回 401 就重取一次再试。"""

    def __init__(self, keys: ServiceKeys, client: tuple[str, str]) -> None:
        self._keys = keys
        self._client = client

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        key = _require(self._keys.get(self._client), self._client)
        request.headers["Authorization"] = f"Bearer {key}"
        response = yield request
        if response.status_code == 401:
            fresh = self._keys.get(self._client, fresh=True)
            if fresh.usable and fresh.key != key:
                request.headers["Authorization"] = f"Bearer {fresh.key}"
                yield request


class PlatformChat:
    """ChatModel：带服务级客户端 Key 调 LiteLLM 的 OpenAI 兼容对话接口。

    ★ Key 不可用、模型不在 Key 里时 complete() 抛 RuntimeError：
      调用方按「LLM 不可用」降级（同没配 LLM）。
    """

    def __init__(
        self,
        litellm_url: str,
        keys: ServiceKeys,
        client: tuple[str, str],
        model: str,
        *,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._keys = keys
        self._client = client
        self._chat = OpenAICompatibleChat(
            f"{litellm_url.rstrip('/')}/v1", "", model, timeout_s=timeout_s, transport=transport
        )

    async def aclose(self) -> None:
        await self._chat.aclose()

    async def _key(self, *, fresh: bool = False) -> str:
        found = await asyncio.to_thread(self._keys.get, self._client, fresh=fresh)
        key = _require(found, self._client)
        if self.model not in found.models:
            raise PlatformUnavailable(
                f"客户端 Key {self._client[0]}:{self._client[1]} 没有分配模型 {self.model}"
            )
        return key

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str:
        kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "json_mode": json_mode,
        }
        self._chat.set_api_key(await self._key())
        try:
            return await self._chat.complete(messages, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
        # Key 刚被重置：重取一次再试
        self._chat.set_api_key(await self._key(fresh=True))
        return await self._chat.complete(messages, **kwargs)
