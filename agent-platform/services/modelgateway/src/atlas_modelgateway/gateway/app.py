"""模型网关的 HTTP 入口（gateway 子命令）。

  通用接口（平台里的应用都用它：不用按模型判断该调哪种格式）
    POST /universal/v1/messages               对话 · 按 Anthropic 格式传；线路的账号没有 Anthropic
                                              格式地址时，网关转成它有的格式（OpenAI、Google）
    POST /universal/v1/messages/count_tokens  数 token（Claude Code 会调）
    GET  /universal/v1/models                 同 /v1/models
  各格式的接口（不转换格式：模型没有这种格式的线路就报错）
    POST /v1/messages               对话 · Anthropic 格式
    POST /v1/messages/count_tokens  数 token
    POST /v1/chat/completions       对话 · OpenAI 格式
    POST /v1/embeddings             向量（OpenAI 格式）
    POST /v1/rerank                 重排（一个问题加一组文档 → 分数）
    GET  /v1/models                 这把 Key 能用的模型和能力
  GET  /healthz /readyz

部署在 Atlas 同一个域名的 /llm 下：Anthropic SDK、Claude Code 的 base_url 填 <域名>/llm/universal
（只走 Anthropic 格式的线路就填 <域名>/llm），OpenAI SDK 填 <域名>/llm/v1。
只开放这些调用接口，不开放任何管理功能。

★ 每个请求从「没有租户」开始，认出 Key 才进入它的租户。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from atlas_redis import PubSubHub, RedisConfig, RedisRuntime
from atlas_tenancy import TenantScope
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from starlette.types import Receive, Scope, Send

from ..crypto import SecretCipher
from ..db.models import GwModelCall
from ..db.session import get_sessionmaker
from ..provider import ProviderCatalog
from ..settings import GatewaySettings, get_settings
from ..shared.health import RouteHealth
from ..shared.kernel import Entry, ErrorCategory, GatewayError, ModelKind
from ..shared.partitions import PartitionKeeper
from ..shared.sync import VersionWatch
from ..shared.upstream import AddressGuard, UpstreamClient
from .admission import Admission, QuotaGuard
from .capability import CapabilityGate
from .config import ConfigSnapshot
from .format import FormatGateway
from .invocation import Answer, Invocation, Timeouts
from .metering import Metering, RecordQueue
from .routing import RouteSelector

__all__ = ["GatewayRuntime", "build_runtime", "create_gateway_app"]

logger = logging.getLogger(__name__)


@dataclass
class GatewayRuntime:
    """网关的各个领域，装配好放在 app.state.gw 上（测试可以换掉其中的部件）。"""

    invocation: Invocation
    admission: Admission
    fmt: FormatGateway
    config: ConfigSnapshot
    records: RecordQueue
    partitions: PartitionKeeper
    health: RouteHealth
    redis: Any | None
    upstream: UpstreamClient


def build_runtime(
    settings: GatewaySettings,
    *,
    redis: Any | None = None,
    pubsub: PubSubHub | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    guard: AddressGuard | None = None,
) -> GatewayRuntime:
    sm = get_sessionmaker()
    watch = VersionWatch(
        redis,
        check_s=settings.config_check_interval_s,
        fallback_s=settings.config_fallback_reload_s,
    )
    config = ConfigSnapshot(
        sm,
        SecretCipher(settings.master_keys()),
        watch,
        pubsub=pubsub,
        negative_ttl_s=settings.key_negative_ttl_s,
    )

    async def _load_monthly_spend(
        tenant_uuid: UUID, key_uuid: UUID, month_start: datetime
    ) -> Decimal:
        with TenantScope.enter(tenant_uuid):
            async with sm() as session:
                total = await session.scalar(
                    select(func.coalesce(func.sum(GwModelCall.cost), 0)).where(
                        GwModelCall.key_uuid == key_uuid,
                        GwModelCall.started_at >= month_start,
                    )
                )
        return Decimal(str(total or 0))

    # ★ 不查租户状态（业务系统不知道它）：租户停用后，用户 Key 的私有模型由「重算有效模型」
    #   任务收回（用户中心回答无权限）；已经发出去的客户端 Key 照常，直到租户被删除
    admission = Admission(config, QuotaGuard(redis, spend_loader=_load_monthly_spend))
    health = RouteHealth(redis)
    upstream = UpstreamClient(
        guard=guard or AddressGuard(settings.upstream_allow_cidrs),
        proxy=settings.egress_proxy,
        transport=transport,
    )
    fmt = FormatGateway(ProviderCatalog(), upstream)
    records = RecordQueue(
        sm,
        batch=settings.record_batch,
        interval_s=settings.record_flush_interval_s,
        max_size=settings.record_queue_max,
    )
    invocation = Invocation(
        admission,
        CapabilityGate(),
        RouteSelector(health),
        fmt,
        Metering(records),
        Timeouts(
            first_content_s=settings.first_content_timeout_s,
            total_first_content_s=settings.total_first_content_timeout_s,
            idle_s=settings.idle_timeout_s,
            keepalive_s=settings.keepalive_interval_s,
            max_attempts=settings.max_attempts,
        ),
    )
    return GatewayRuntime(
        invocation,
        admission,
        fmt,
        config,
        records,
        PartitionKeeper(sm),
        health,
        redis,
        upstream,
    )


def _secret(request: Request) -> str | None:
    """Anthropic SDK 用 x-api-key；Claude Code（AUTH_TOKEN）、OpenAI SDK 用 Authorization。"""
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _error_response(fmt: FormatGateway, entry: Entry, err: GatewayError) -> Response:
    headers = {}
    if err.no_retry:
        headers["x-should-retry"] = "false"
    if err.retry_after_s:
        headers["retry-after"] = str(int(err.retry_after_s))
    if err.upstream_body is not None and err.status == 400:
        # 请求有误：上游原样的错误体（调用方能看到具体哪个字段不对）
        return Response(
            err.upstream_body, status_code=400, media_type="application/json", headers=headers
        )
    return Response(
        fmt.error_body(entry, err),
        status_code=err.status,
        media_type="application/json",
        headers=headers,
    )


class LazyStream(Response):
    """流式响应：等到第一段（内容或保活注释）才发 200 和响应头。

    在那之前出错（准入、全部线路都失败），回正常的错误状态码；调用方断开就停下、关上游。
    """

    def __init__(
        self,
        stream: AsyncIterator[bytes],
        on_error: Callable[[GatewayError], Response],
        headers: dict[str, str],
    ) -> None:
        self._stream = stream
        self._on_error = on_error
        self._headers = headers
        self.status_code = 200
        self.background = None
        self.raw_headers = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        pump = asyncio.ensure_future(self._pump(scope, receive, send))
        watch = asyncio.ensure_future(self._watch(receive))
        try:
            done, _ = await asyncio.wait({pump, watch}, return_when=asyncio.FIRST_COMPLETED)
            if pump not in done:
                pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        finally:
            watch.cancel()
            await self._stream.aclose()

    async def _watch(self, receive: Receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

    async def _pump(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            first = await self._stream.__anext__()
        except StopAsyncIteration:
            first = b""
        except GatewayError as err:
            await self._on_error(err)(scope, receive, send)
            return
        headers = [(b"content-type", b"text/event-stream; charset=utf-8")]
        headers += [(k.encode(), v.encode()) for k, v in self._headers.items()]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": first, "more_body": True})
        async for chunk in self._stream:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class _Isolated:
    """每个请求从「没有租户」开始。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with TenantScope.isolated():
            await self.app(scope, receive, send)


def create_gateway_app(
    settings: GatewaySettings | None = None,
    *,
    runtime: GatewayRuntime | None = None,
    redis_factory: Callable[[str], Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_redis = None
        redis_rt: RedisRuntime | None = None
        rt = runtime
        if rt is None:
            if settings.redis_url:
                if redis_factory is not None:
                    owned_redis = redis_factory(settings.redis_url)
                else:
                    redis_rt = RedisRuntime(RedisConfig(url=settings.redis_url, service="modelgw"))
                    redis_rt.pubsub.start()
                    owned_redis = redis_rt.commands
            rt = build_runtime(
                settings,
                redis=owned_redis,
                pubsub=redis_rt.pubsub if redis_rt is not None else None,
            )
        app.state.gw = rt
        app.state.ready = False
        await rt.partitions.ensure()
        rt.records.start()
        rt.config.start_invalidation_listener()
        keeper = asyncio.get_running_loop().create_task(_daily(rt.partitions))
        app.state.ready = True
        try:
            yield
        finally:
            keeper.cancel()
            await rt.config.stop_invalidation_listener()
            await rt.records.stop()
            if runtime is None:
                await rt.upstream.aclose()
                if redis_rt is not None:
                    await redis_rt.aclose()
                elif owned_redis is not None:
                    await owned_redis.aclose()

    app = FastAPI(title="Model Gateway", version="1.0.0", lifespan=lifespan)
    app.add_middleware(_Isolated)

    async def _body(request: Request) -> dict[str, Any]:
        try:
            data = json.loads(await request.body())
        except ValueError as exc:
            raise GatewayError(ErrorCategory.BAD_REQUEST, "请求体不是合法的 JSON") from exc
        if not isinstance(data, dict):
            raise GatewayError(ErrorCategory.BAD_REQUEST, "请求体必须是 JSON 对象")
        return data

    async def _run(request: Request, entry: Entry, kind: ModelKind) -> Response:
        rt: GatewayRuntime = request.app.state.gw
        try:
            body = await _body(request)
            answer: Answer = await rt.invocation.call(
                entry, kind, _secret(request), body, dict(request.headers)
            )
        except GatewayError as err:
            return _error_response(rt.fmt, entry, err)
        if answer.stream is not None:
            return LazyStream(
                answer.stream, lambda e: _error_response(rt.fmt, entry, e), answer.headers
            )
        return Response(
            answer.body,
            status_code=answer.status,
            media_type="application/json",
            headers=answer.headers,
        )

    async def _count_tokens(request: Request, entry: Entry) -> Response:
        rt: GatewayRuntime = request.app.state.gw
        try:
            body = await _body(request)
            result = await rt.invocation.count_tokens(
                entry, _secret(request), body, dict(request.headers)
            )
        except GatewayError as err:
            return _error_response(rt.fmt, entry, err)
        return JSONResponse(result)

    @app.post("/universal/v1/messages")
    async def universal_messages(request: Request) -> Response:
        return await _run(request, Entry.UNIVERSAL, ModelKind.CHAT)

    @app.post("/universal/v1/messages/count_tokens")
    async def universal_count_tokens(request: Request) -> Response:
        return await _count_tokens(request, Entry.UNIVERSAL)

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await _run(request, Entry.ANTHROPIC, ModelKind.CHAT)

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        return await _count_tokens(request, Entry.ANTHROPIC)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _run(request, Entry.OPENAI, ModelKind.CHAT)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await _run(request, Entry.OPENAI, ModelKind.EMBEDDING)

    @app.post("/v1/rerank")
    async def rerank(request: Request) -> Response:
        return await _run(request, Entry.OPENAI, ModelKind.RERANK)

    @app.get("/v1/models")
    @app.get("/universal/v1/models")
    async def models(request: Request) -> Response:
        rt: GatewayRuntime = request.app.state.gw
        try:
            entries = await rt.admission.models_of(_secret(request))
        except GatewayError as err:
            return _error_response(rt.fmt, Entry.OPENAI, err)
        data = [
            {
                "id": m.name,
                "type": "model",
                "object": "model",
                "display_name": m.display_name,
                "owned_by": "atlas",
                "kind": m.kind.value,
                "description": m.description,
                "capabilities": m.caps.to_json(),
            }
            for m in entries
        ]
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        ready = getattr(request.app.state, "ready", False)
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    return app


async def _daily(partitions: PartitionKeeper) -> None:
    while True:
        await asyncio.sleep(86_400)
        try:
            await partitions.ensure()
        except Exception:
            logger.warning("建调用记录的分区失败，明天再试", exc_info=True)
