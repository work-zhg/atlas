"""遥测接入（与 atlas-server 的可观测性设计同一套约定）。

    知识库服务 ──OTLP/HTTP──▶ OpenTelemetry Collector ──▶ Jaeger（全量链路，不带正文）
                                                    └──▶ Langfuse（带正文与否由档位决定）

★ 关闭时零副作用：不装 TracerProvider，opentelemetry-api 的全局 tracer 是 no-op，
  埋点处不写 if enabled。装不上也不拦着进程起（遥测是旁路），但要喊出来。
★ 链路串联：请求头里的 traceparent 由 trace_requests 中间件取出，检索 span 挂在
  atlas-server 那次调用之下 —— 一条对话链路里能看到「智能体 → 检索内部 → 生成」。
★ 属性分两类：
    结构化（id、分数、排名、数量）  普通属性，Jaeger 也看得到
    正文（查询、段落内容）          只写 langfuse.observation.input/output，过 content() 判档位
  另标 OpenInference 的 span 类型（RETRIEVER / RERANKER / EMBEDDING），便于接 Phoenix 一类后端。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .settings import KnowledgeSettings

__all__ = [
    "content",
    "current_trace_id",
    "set_content",
    "setup_telemetry",
    "shutdown_telemetry",
    "trace_requests",
    "tracer",
]

logger = logging.getLogger(__name__)

_provider: Any = None
_capture = "off"
_max_chars = 32_768
_ORDER = {"off": 0, "io": 1, "full": 2}

#: OpenInference 的 span 类型属性名
SPAN_KIND = "openinference.span.kind"


def setup_telemetry(settings: KnowledgeSettings, *, service_name: str | None = None) -> None:
    global _provider, _capture, _max_chars
    _capture = settings.otel_capture_content
    _max_chars = settings.otel_content_max_chars
    if not settings.otel_enabled or _provider is not None:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name or settings.otel_service_name}),
            sampler=_DropHealth(),
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
            )
        )
        trace.set_tracer_provider(provider)
        _provider = provider
        logger.info("遥测已启用 → %s（内容档位 %s）", settings.otel_endpoint, _capture)
    except Exception:
        logger.exception("遥测装配失败，本进程将不产生 span")


class _DropHealth:
    """探针的 /health 每几秒一次，不进链路（FastAPI 自带的埋点没有排除路径的选项）。

    ★ 按 url.path 判断：FastAPI 建 span 时还没匹配路由，名字只是「GET」，之后才改成「GET /health」。
    """

    def should_sample(
        self,
        parent_context: Any,
        trace_id: int,
        name: str,
        kind: Any = None,
        attributes: Any = None,
        *a: Any,
        **kw: Any,
    ) -> Any:
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, SamplingResult

        parent = trace.get_current_span(parent_context).get_span_context()
        # 子 span 跟父走（被丢掉的 /health 下面的 fastapi.endpoint 等也一起丢）
        if parent.is_valid and not parent.trace_flags.sampled:
            return SamplingResult(Decision.DROP)
        if not parent.is_valid and (attributes or {}).get("url.path") == "/health":
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(parent_context, trace_id, name, kind, attributes, *a, **kw)

    def get_description(self) -> str:
        return "DropHealth"


def shutdown_telemetry() -> None:
    """退出前把缓冲里的 span 刷出去 —— 最后一批往往正是出问题那批。"""
    global _provider
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception:
        logger.warning("遥测 flush 失败", exc_info=True)
    finally:
        _provider = None


def tracer() -> trace.Tracer:
    return trace.get_tracer("atlas_knowledge")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def content(value: Any, *, level: str = "io") -> str | None:
    """档位够才返回（截断后的）JSON 文本；不够返回 None，调用方据此不写属性。"""
    if _ORDER[_capture] < _ORDER[level]:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _max_chars:
        text = text[:_max_chars] + f"…（已截断，原长 {len(text)}）"
    return text


def set_content(
    span: trace.Span, *, input: Any = None, output: Any = None, level: str = "io"
) -> None:
    if input is not None and (v := content(input, level=level)) is not None:
        span.set_attribute("langfuse.observation.input", v)
    if output is not None and (v := content(output, level=level)) is not None:
        span.set_attribute("langfuse.observation.output", v)


def trace_requests(app: Any) -> None:
    """给每个 HTTP 请求开一个 SERVER span，父上下文取自请求头的 traceparent。

    ★ FastAPI ≥ 0.142 自带 OpenTelemetry 埋点（fastapi.telemetry：SERVER span、取 traceparent、
      span 名用路由模板）—— 有它就不再装这个中间件，否则每个请求两个 SERVER span。
    """
    try:
        import fastapi.telemetry  # noqa: F401
    except ImportError:
        pass
    else:
        return

    @app.middleware("http")
    async def _trace(request: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if request.url.path == "/health":
            return await call_next(request)
        ctx = propagate.extract(dict(request.headers))
        with tracer().start_as_current_span(
            f"{request.method} {request.url.path}", context=ctx, kind=SpanKind.SERVER
        ) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            return response
