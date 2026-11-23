"""可插拔 Redis 中间件拦截器链（c4-model/infra-platform/code-redis.html）。

参考 Go go-redis 的 Hook 链设计：每次单命令、Pipeline 或脚本执行都封装成 CommandContext，
按「外 → 内」顺序穿过中间件栈：

    GuardMiddleware → TelemetryMiddleware → ResilienceMiddleware → 底层连接池

★ 为什么按这个顺序：
  1. Guard 在最外层：危险命令 / 超大写入立刻拒绝，既不记入慢日志，也不触发熔断器计数；
  2. Telemetry 包着 Resilience：指标和 Span 记录的是调用方感知到的真实端到端耗时与成败；
  3. Resilience 在最内层：最贴近网络传输，负责幂等读瞬断重试与熔断短路。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .errors import CircuitOpen, CommandForbidden, PayloadTooLarge, RedisUnavailable

__all__ = [
    "FAIL_OPEN_FALLBACK",
    "CircuitBreaker",
    "CommandContext",
    "GuardMiddleware",
    "NextCall",
    "RedisMiddleware",
    "ResilienceMiddleware",
    "TelemetryMiddleware",
    "TelemetrySnapshot",
]

logger = logging.getLogger(__name__)

#: Fail-Open 短路或降级时返回的哨兵：由上层原语识别并转为各自安全的降级值
FAIL_OPEN_FALLBACK: Any = object()

#: 禁止在业务路径执行的危险命令
DEFAULT_FORBIDDEN_COMMANDS: frozenset[str] = frozenset(
    {
        "KEYS",
        "FLUSHDB",
        "FLUSHALL",
        "MONITOR",
        "DEBUG",
        "SHUTDOWN",
        "SLAVEOF",
        "REPLICAOF",
        "CONFIG",
    }
)

#: 明确幂等、且可在瞬时断连时安全重试一次的只读命令
IDEMPOTENT_READ_COMMANDS: frozenset[str] = frozenset(
    {
        "GET",
        "MGET",
        "EXISTS",
        "TTL",
        "PTTL",
        "HGET",
        "HGETALL",
        "HMGET",
        "SISMEMBER",
        "SMEMBERS",
        "SCARD",
        "ZSCORE",
        "ZCARD",
        "XRANGE",
        "XREVRANGE",
        "XLEN",
        "PING",
    }
)

#: 判定为传输层故障、计入熔断器的异常集合
_TRANSPORT_ERRORS = (RedisConnectionError, RedisTimeoutError, TimeoutError, OSError)


def _arg_bytes(value: Any) -> int:
    if isinstance(value, bytes | bytearray | memoryview):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return 0


def estimate_payload_bytes(command: str, args: Sequence[Any]) -> int:
    """估算单次命令写入载荷大小（只统计可能带大 value 的写命令，避免读路径白算）。"""
    if command in IDEMPOTENT_READ_COMMANDS:
        return 0
    return sum(_arg_bytes(a) for a in args)


@dataclass(slots=True)
class CommandContext:
    """穿过拦截器链的命令上下文。"""

    service: str
    command: str
    args: tuple[Any, ...] = ()
    keys: tuple[str, ...] = ()
    key_pattern: str = ""
    fail_open: bool = False
    is_pipeline: bool = False
    subcommands: tuple[str, ...] = ()
    payload_bytes: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


NextCall = Callable[[CommandContext], Awaitable[Any]]


class RedisMiddleware(Protocol):
    """中间件协议：接收上下文和下游回调，返回执行结果。"""

    async def __call__(self, ctx: CommandContext, call_next: NextCall) -> Any: ...


class GuardMiddleware:
    """执行前护栏：拦截危险命令、对大写入告警或硬拒绝。"""

    def __init__(
        self,
        *,
        forbid_dangerous: bool = True,
        forbidden_commands: frozenset[str] = DEFAULT_FORBIDDEN_COMMANDS,
        warn_bytes: int = 256 * 1024,
        max_bytes: int | None = 4 * 1024 * 1024,
    ) -> None:
        self._forbid = forbid_dangerous
        self._forbidden = forbidden_commands
        self._warn_bytes = warn_bytes
        self._max_bytes = max_bytes

    async def __call__(self, ctx: CommandContext, call_next: NextCall) -> Any:
        if self._forbid:
            names = ctx.subcommands if ctx.is_pipeline else (ctx.command,)
            for name in names:
                upper = name.upper()
                if upper in self._forbidden:
                    raise CommandForbidden(upper)

        size = ctx.payload_bytes or estimate_payload_bytes(ctx.command, ctx.args)
        if self._max_bytes is not None and size > self._max_bytes:
            raise PayloadTooLarge(ctx.command, size, self._max_bytes)
        if self._warn_bytes > 0 and size > self._warn_bytes:
            logger.warning(
                "Redis 写入载荷偏大：service=%s cmd=%s pattern=%s bytes=%d",
                ctx.service,
                ctx.command,
                ctx.key_pattern or "<unspecified>",
                size,
            )
        return await call_next(ctx)


@dataclass
class TelemetrySnapshot:
    total_commands: int = 0
    errors: int = 0
    slow_commands: int = 0
    degraded: int = 0


def _try_get_tracer() -> Any | None:
    try:
        from opentelemetry import trace

        return trace.get_tracer("atlas_redis")
    except Exception:
        return None


class TelemetryMiddleware:
    """统一可观测：OTel Span（可选）、慢命令日志与进程内指标计数。

    ★ 只记脱敏后的 key_pattern，绝不把 args / value 放进日志或 Span，避免敏感凭据泄露与基数爆炸。
    """

    def __init__(self, *, slow_ms: float = 50.0, enable_otel: bool = True) -> None:
        self._slow_ms = slow_ms
        self._tracer = _try_get_tracer() if enable_otel else None
        self.stats = TelemetrySnapshot()

    async def __call__(self, ctx: CommandContext, call_next: NextCall) -> Any:
        self.stats.total_commands += 1
        started = time.monotonic()
        span_cm = (
            self._tracer.start_as_current_span(f"redis.{ctx.command.lower()}")
            if self._tracer is not None
            else None
        )
        if span_cm is None:
            return await self._run(ctx, call_next, started, None)
        with span_cm as span:
            span.set_attribute("db.system", "redis")
            span.set_attribute("db.operation", ctx.command)
            span.set_attribute("atlas.redis.service", ctx.service)
            if ctx.key_pattern:
                span.set_attribute("atlas.redis.key_pattern", ctx.key_pattern)
            return await self._run(ctx, call_next, started, span)

    async def _run(
        self, ctx: CommandContext, call_next: NextCall, started: float, span: Any | None
    ) -> Any:
        try:
            result = await call_next(ctx)
        except Exception as exc:
            self.stats.errors += 1
            if span is not None:
                span.record_exception(exc)
            raise
        else:
            if result is FAIL_OPEN_FALLBACK:
                self.stats.degraded += 1
            return result
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if span is not None:
                span.set_attribute("atlas.redis.duration_ms", round(elapsed_ms, 2))
            if elapsed_ms >= self._slow_ms:
                self.stats.slow_commands += 1
                logger.warning(
                    "Redis 慢命令：service=%s cmd=%s pattern=%s duration_ms=%.1f",
                    ctx.service,
                    ctx.command,
                    ctx.key_pattern or "<unspecified>",
                    elapsed_ms,
                )


class CircuitBreaker:
    """轻量单进程熔断器（CLOSED → OPEN → HALF_OPEN）。

    ★ 目的不是替 Redis 做 HA，而是在 Redis 不可达时避免所有软状态请求（限流、缓存、广播）
      全部卡满 2s 超时，拖垮整个 FastAPI 工作进程。
    """

    def __init__(
        self,
        *,
        service: str,
        failure_threshold: int = 5,
        recovery_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._threshold = failure_threshold
        self._recovery_s = recovery_s
        self._clock = clock
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        if self._state == "open" and (self._clock() - self._opened_at) >= self._recovery_s:
            return "half_open"
        return self._state

    def before_call(self) -> None:
        """判断是否放行；开启期间直接抛 CircuitOpen。"""
        now = self._clock()
        if self._state == "open":
            elapsed = now - self._opened_at
            if elapsed < self._recovery_s:
                raise CircuitOpen(self._service, self._recovery_s - elapsed)
            self._state = "half_open"
            self._probe_in_flight = False
        if self._state == "half_open":
            if self._probe_in_flight:
                raise CircuitOpen(self._service, 0.5)
            self._probe_in_flight = True

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"
        self._probe_in_flight = False

    def on_failure(self) -> None:
        self._probe_in_flight = False
        if self._state == "half_open":
            self._state = "open"
            self._opened_at = self._clock()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = "open"
            self._opened_at = self._clock()
            logger.error(
                "Redis 熔断器开启：service=%s consecutive_failures=%d recovery_s=%.1f",
                self._service,
                self._failures,
                self._recovery_s,
            )


class ResilienceMiddleware:
    """故障韧性：幂等读瞬断单次重试 + 熔断器 + Fail-Open / Fail-Closed 分流。"""

    def __init__(
        self,
        breaker: CircuitBreaker,
        *,
        retry_backoff_s: float = 0.02,
        idempotent_reads: frozenset[str] = IDEMPOTENT_READ_COMMANDS,
    ) -> None:
        self.breaker = breaker
        self._retry_backoff_s = retry_backoff_s
        self._idempotent_reads = idempotent_reads

    async def __call__(self, ctx: CommandContext, call_next: NextCall) -> Any:
        try:
            self.breaker.before_call()
        except CircuitOpen as exc:
            if ctx.fail_open:
                logger.warning(
                    "Redis 熔断中，软状态降级：service=%s cmd=%s", ctx.service, ctx.command
                )
                return FAIL_OPEN_FALLBACK
            raise exc

        can_retry = (not ctx.is_pipeline) and (ctx.command.upper() in self._idempotent_reads)
        attempts = 2 if can_retry else 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                result = await call_next(ctx)
            except _TRANSPORT_ERRORS as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    if self._retry_backoff_s > 0:
                        await asyncio.sleep(self._retry_backoff_s)
                    continue
                self.breaker.on_failure()
                if ctx.fail_open:
                    logger.warning(
                        "Redis 传输异常，软状态降级：service=%s cmd=%s err=%s",
                        ctx.service,
                        ctx.command,
                        exc,
                    )
                    return FAIL_OPEN_FALLBACK
                raise RedisUnavailable(f"Redis {ctx.command} 失败：{exc}") from exc
            else:
                self.breaker.on_success()
                return result

        # 理论上不可达，兜底满足类型检查
        assert last_exc is not None
        raise RedisUnavailable(f"Redis {ctx.command} 失败：{last_exc}") from last_exc
