"""遥测接入（可观测性设计 §02）。

    Server ──OTLP/HTTP──▶ OpenTelemetry Collector ──▶ Jaeger（全量链路）
                                                 └──▶ Langfuse（LLM 维度，可选）

★ 只往 Collector 发，不直连任何后端。分流与脱敏都在 Collector 里做 ——
  于是「要不要 Langfuse」是一份 YAML 的事，server 里一行代码都不用改。

★ 关闭时是**真正的零副作用**：不装 TracerProvider，opentelemetry-api 的
  全局 tracer 退化成官方保证的 no-op 实现。所以埋点处不写 `if enabled`，
  照常 start_span 即可 —— 少一堆分支，也不会出现「忘了判断」的路径。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

__all__ = ["setup_telemetry", "shutdown_telemetry", "tracer"]

#: 本模块装上去的 provider。None = 没启用（或启用失败）。
_provider = None


def setup_telemetry(settings: Settings) -> None:
    """按配置装 TracerProvider。未启用则什么都不做。

    ★ 装不上**不让进程起不来**。遥测是旁路：Collector 没起来、地址写错，
      都不该让用户的对话跑不了。与 cluster 后端那种「宁可起不来」的取舍
      相反，因为那边装不上等于功能静默失效，这边只是少了可观测性。
      代价是要把失败**喊出来**，否则就成了「以为在采集其实没有」。
    """
    global _provider
    if not settings.otel_enabled:
        logger.info("遥测未启用（otel_enabled=false）")
        return
    if _provider is not None:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        )
        # ★ 采样用默认的 parent-based always-on。采样策略（放 SDK 侧还是
        #   Collector 侧）是 §11 第 6 项的待验证项，本机量级不需要先决定 ——
        #   而且过早在 SDK 侧采样会让 Collector 拿不到全量，回不了头。
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
            )
        )
        trace.set_tracer_provider(provider)
        _provider = provider
        logger.info("遥测已启用 → %s", settings.otel_endpoint)
    except Exception:
        # 喊出来，但不拦着进程起
        logger.exception("遥测装配失败，本进程将不产生 span")


def shutdown_telemetry() -> None:
    """退出前把缓冲里的 span 刷出去。

    ★ 不刷的话最后一段 trace 会凭空消失 —— BatchSpanProcessor 是按批发的，
      进程直接退出时那一批还在内存里。而「最后一段」往往正是出问题那段。
    """
    global _provider
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception:
        logger.warning("遥测 flush 失败", exc_info=True)
    finally:
        _provider = None


def tracer():
    """取全局 tracer。未启用时返回 no-op 实现，调用方不必判断。"""
    from opentelemetry import trace

    return trace.get_tracer("atlas.server")
