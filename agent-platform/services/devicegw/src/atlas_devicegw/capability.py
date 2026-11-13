"""会话登记与能力代理（c4-model/agent/devicegw/code-devicegw-capability.html）。

    SessionRegistry   Agent 服务每轮登记一个会话的真实上游和启动参数，换回会话令牌和网关上的地址
    CapabilityProxy   本机 CLI 拿会话令牌来调模型、MCP、知识库：认令牌 → 换凭据 → 流式转发
    HeaderPolicy      改请求头只写这一处

★ 真实凭据只在这里，从不交给设备：智能体的 Key 是它所有使用者共用的，放到个人电脑上就等于交出去了。
★ 会话令牌整个 CLI 会话不变（CLI 启动时读一次），上游每轮整份覆盖：换 Key、收回授权不用重启 CLI。
★ 令牌原文存在登记里：连接器重启后要重新拉起 bridge，下一次开会话流还得把它放进启动参数。登记里本来
  就存着比它更敏感的真实凭据，二者同等保护（只在集群内的 Redis，有过期）。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .store import BindingStore

__all__ = ["CapabilityProxy", "HeaderPolicy", "SessionRegistry", "token_hash"]

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "dst_"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionRegistry:
    def __init__(self, store: BindingStore, public_url: str) -> None:
        self._store = store
        self._public = public_url.rstrip("/")

    def endpoint(self, upstream_id: str) -> str:
        return f"{self._public}/u/{upstream_id}"

    async def register(
        self,
        thread: str,
        *,
        device: str,
        owner: str,
        launch: dict[str, Any],
        upstreams: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """登记本轮能力。没有登记（或换了设备）就生成会话令牌；有就沿用，整份覆盖上游和启动参数。"""
        existing = await self._store.by_thread(thread)
        if existing is not None and existing["device"] == device and existing["owner"] == owner:
            token = existing["token"]
        else:
            if existing is not None:
                await self._store.delete(thread)
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        binding = {
            "thread": thread,
            "device": device,
            "owner": owner,
            "launch": launch,
            "upstreams": {
                u["id"]: {
                    "kind": u.get("kind", ""),
                    "url": u["url"],
                    "headers": u.get("headers") or {},
                }
                for u in upstreams
            },
            "token": token,
            "token_hash": token_hash(token),
        }
        await self._store.save(binding)
        return {
            "token": token,
            "endpoints": {uid: self.endpoint(uid) for uid in binding["upstreams"]},
        }

    async def get(self, thread: str) -> dict[str, Any] | None:
        return await self._store.by_thread(thread)

    async def revoke(self, thread: str) -> None:
        await self._store.delete(thread)

    async def revoke_device(self, device: str) -> list[str]:
        threads = await self._store.threads_of(device)
        for thread in threads:
            await self._store.delete(thread)
        return threads


class HeaderPolicy:
    """设备带来的认证和身份一律去掉，换上上游的真实凭据。

    身份由 Agent 服务登记时写进上游的请求头。
    """

    #: 设备不能带进来的：认证、身份、逐跳的
    STRIP = frozenset(
        {
            "authorization",
            "x-api-key",
            "proxy-authorization",
            "cookie",
            "x-uid",
            "x-auth-uid",
            "x-tenant-id",
            "x-caller-app",
            "x-knowledge-token",
            "x-atlas-knowledge-token",
            "host",
            "content-length",
            "connection",
            "keep-alive",
            "proxy-connection",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }
    )
    #: 回给设备时去掉的（逐跳的；长度由流式响应重新决定）
    STRIP_BACK = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length"})

    @classmethod
    def outbound(cls, incoming: Any, upstream: dict[str, Any]) -> dict[str, str]:
        headers = {k: v for k, v in incoming.items() if k.lower() not in cls.STRIP}
        headers.update(upstream.get("headers") or {})
        return headers

    @classmethod
    def back(cls, headers: httpx.Headers) -> dict[str, str]:
        return {k: v for k, v in headers.items() if k.lower() not in cls.STRIP_BACK}


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    key = request.headers.get("x-api-key", "").strip()
    return key or None


class CapabilityProxy:
    def __init__(self, store: BindingStore, client: httpx.AsyncClient) -> None:
        self._store = store
        self._client = client

    async def handle(self, request: Request, upstream_id: str, path: str) -> Response:
        token = _bearer(request)
        if token is None:
            return _error(401, "unauthorized", "缺少会话令牌")
        binding = await self._store.by_token(token_hash(token))
        if binding is None:
            return _error(401, "unauthorized", "会话令牌无效或已作废")
        upstream = binding["upstreams"].get(upstream_id)
        if upstream is None:
            return _error(404, "not_found", f"这个会话没有上游 {upstream_id}")
        await self._store.touch(binding)
        target = upstream["url"] if not path else upstream["url"].rstrip("/") + "/" + path
        if request.url.query:
            target += "?" + request.url.query
        outbound = self._client.build_request(
            request.method,
            target,
            headers=HeaderPolicy.outbound(request.headers, upstream),
            content=request.stream(),
        )
        try:
            resp = await self._client.send(outbound, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("能力代理连不上上游 %s：%s", upstream_id, exc)
            return _error(502, "upstream_unreachable", "上游服务暂时不可用")
        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers=HeaderPolicy.back(resp.headers),
            background=BackgroundTask(resp.aclose),
        )


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"kind": kind, "message": message}}, status_code=status)
