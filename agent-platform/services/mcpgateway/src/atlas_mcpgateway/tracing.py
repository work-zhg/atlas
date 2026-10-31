"""Trace（OpenTelemetry）。只接 Trace：不做调用记录、指标。

    服务端 Span   HTTP 级一个（dataplane.py）；每条消息一个，MCP SDK 自带，名字如
                 「tools/call 工具名」，属性按 OTel 的 MCP 语义约定（mcp.method.name、
                 gen_ai.tool.name、jsonrpc.request.id …）
    网关属性      加在当前 Span 上：mcp.gateway.service、mcp.gateway.key_uid、mcp.gateway.outcome；
                 被拒的请求也有 Span，带 error.type
    客户端 Span   调 REST / gRPC 上游时（upstreams/hub.py）；MCP 上游由 MCP SDK 的客户端出
    上下文传递    进：HTTP 头 traceparent，没有时取 params._meta；
                 出：HTTP 头 / gRPC metadata，MCP 上游另外写进 params._meta

导出：设置了 OTEL_EXPORTER_OTLP_ENDPOINT（或 …_TRACES_ENDPOINT）时，用 OTLP/HTTP 发给 Collector；
采样跟随 OTEL_TRACES_SAMPLER（缺省 parentbased_always_on）。没设置时只传递上下文、不导出。

★ 不记录：工具参数和结果（可能含个人信息、业务数据）、Key 原文、上游凭据。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from opentelemetry import context, propagate, trace

logger = logging.getLogger(__name__)

__all__ = ["attach_incoming", "detach", "mark", "setup"]

_configured = False


def setup(service_name: str = "mcp-gateway") -> bool:
    """按标准环境变量配置导出。→ 有没有配置导出。只配一次。"""
    global _configured
    if _configured:
        return True
    if not (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME") or service_name}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info("Trace 经 OTLP 导出")
    return True


def attach_incoming(
    headers: Mapping[str, str], meta: Mapping[str, Any] | None = None
) -> object | None:
    """入站的追踪上下文 → 当前上下文。HTTP 头 traceparent 优先，没有时取 params._meta 里的
    （MCP SDK 的客户端只写 _meta）：网关的 HTTP 级 Span 和 SDK 的消息 Span 在同一条 trace 里。"""
    carrier = {
        k.lower(): v for k, v in headers.items() if k.lower() in ("traceparent", "tracestate")
    }
    if "traceparent" not in carrier and meta and isinstance(meta.get("traceparent"), str):
        carrier = {k: str(meta[k]) for k in ("traceparent", "tracestate") if meta.get(k)}
    if "traceparent" not in carrier:
        return None
    return context.attach(propagate.extract(carrier))


def detach(token: object | None) -> None:
    if token is not None:
        context.detach(token)  # type: ignore[arg-type]


def mark(**attributes: Any) -> None:
    """给当前 Span 加网关的属性（mcp.gateway.*）。值为 None 的不加。"""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(f"mcp.gateway.{key}", value)
