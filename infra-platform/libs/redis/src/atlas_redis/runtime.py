"""RedisRuntime：三类连接舱壁隔离与进程级生命周期入口（c4-model/infra-platform/code-redis.html）。

每个业务服务在组合根 / FastAPI lifespan 中持有一份 RedisRuntime 单例：
  · commands  短命令池（socket_timeout=2s）+ Guard / Telemetry / Resilience 中间件链
  · blocking  阻塞读池（socket_timeout=None）专供 XREAD BLOCK / BLPOP
  · pubsub    单连接多路复用 PubSubHub（进程内仅占 1 条长连接）
  · scripts   Lua 脚本注册中心（EVALSHA 自动回退）

★ 调用方拿 runtime.commands / runtime.blocking 来用、绝不手动 aclose()；统一由 lifespan 退出关闭。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, TypeVar

import redis.asyncio as aioredis
from atlas_redislock import RedisLock

from .cache import CoalescedCache
from .client import RedisClient
from .config import RedisConfig
from .keyspace import KeySpace
from .middleware import (
    CircuitBreaker,
    GuardMiddleware,
    RedisMiddleware,
    ResilienceMiddleware,
    TelemetryMiddleware,
)
from .pubsub import PubSubHub
from .quota import AtomicQuota
from .scripts import ScriptRegistry
from .stream import StreamChannel

__all__ = ["RedisRuntime"]

T = TypeVar("T")


def _build_client(
    config: RedisConfig, *, max_connections: int, socket_timeout: float | None
) -> aioredis.Redis:
    pool = aioredis.BlockingConnectionPool.from_url(
        config.url,
        max_connections=max_connections,
        timeout=config.pool_wait_timeout_s,
        decode_responses=config.decode_responses,
        socket_timeout=socket_timeout,
        socket_connect_timeout=config.command_connect_timeout_s,
        health_check_interval=config.health_check_interval_s,
    )
    return aioredis.Redis(connection_pool=pool)


def _pool_usage(raw_client: Any) -> dict[str, Any]:
    pool = getattr(raw_client, "connection_pool", None)
    if pool is None:
        return {}
    max_conns = getattr(pool, "max_connections", None)
    in_use = len(getattr(pool, "_in_use_connections", ()) or ())
    created = getattr(pool, "_created_connections", None)
    return {
        "max_connections": max_conns,
        "in_use": in_use,
        "created": created,
    }


class RedisRuntime:
    """进程级 Redis 中间件总入口。"""

    def __init__(
        self,
        config: RedisConfig,
        *,
        extra_middlewares: Sequence[RedisMiddleware] = (),
        enable_otel: bool = True,
        _commands_raw: Any | None = None,
        _blocking_raw: Any | None = None,
        _pubsub_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._owns_raw = _commands_raw is None and _blocking_raw is None

        self._commands_raw = _commands_raw or _build_client(
            config,
            max_connections=config.command_max_connections,
            socket_timeout=config.command_socket_timeout_s,
        )
        self._blocking_raw = _blocking_raw or _build_client(
            config,
            max_connections=config.blocking_max_connections,
            socket_timeout=config.blocking_socket_timeout_s,
        )

        # 中间件链顺序：Guard → Telemetry → (用户自定义) → Resilience
        self.guard = GuardMiddleware(
            forbid_dangerous=config.forbid_dangerous_commands,
            warn_bytes=config.large_payload_warn_bytes,
            max_bytes=config.large_payload_max_bytes,
        )
        self.telemetry = TelemetryMiddleware(
            slow_ms=config.slow_command_ms,
            enable_otel=enable_otel,
        )
        self.breaker = CircuitBreaker(
            service=config.service,
            failure_threshold=config.breaker_failure_threshold,
            recovery_s=config.breaker_recovery_s,
        )
        self.resilience = ResilienceMiddleware(self.breaker)

        command_mws: tuple[RedisMiddleware, ...] = (
            self.guard,
            self.telemetry,
            *extra_middlewares,
            self.resilience,
        )
        # blocking 池不做熔断器计数（长阻塞读由业务流控），仅保留护栏与遥测
        blocking_mws: tuple[RedisMiddleware, ...] = (self.guard, self.telemetry)

        self.commands = RedisClient(
            self._commands_raw, service=config.service, middlewares=command_mws
        )
        self.blocking = RedisClient(
            self._blocking_raw, service=config.service, middlewares=blocking_mws
        )
        self.scripts = ScriptRegistry(self.commands)

        self._pubsub_raw: aioredis.Redis | None = None

        def _default_pubsub_factory() -> Any:
            if self._pubsub_raw is None:
                self._pubsub_raw = aioredis.from_url(
                    config.url,
                    decode_responses=config.decode_responses,
                    socket_timeout=None,
                    socket_connect_timeout=config.command_connect_timeout_s,
                    health_check_interval=config.health_check_interval_s,
                )
            return self._pubsub_raw.pubsub()

        self.pubsub = PubSubHub(
            commands=self.commands,
            pubsub_factory=_pubsub_factory or _default_pubsub_factory,
            queue_maxsize=config.pubsub_queue_maxsize,
            backoff_min_s=config.pubsub_backoff_min_s,
            backoff_max_s=config.pubsub_backoff_max_s,
        )

    # ------------------------------------------------------------------ 生命周期

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[RedisRuntime]:
        """FastAPI lifespan 集成：启动 PubSubHub，退出时先停 PubSub 再安全关池。"""
        self.pubsub.start()
        try:
            yield self
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """优雅释放全部连接池与后台订阅连接。"""
        await self.pubsub.stop()
        if self._pubsub_raw is not None:
            with contextlib.suppress(Exception):
                await self._pubsub_raw.aclose()
            self._pubsub_raw = None
        if self._owns_raw:
            for client in (self._commands_raw, self._blocking_raw):
                with contextlib.suppress(Exception):
                    await client.aclose()
                pool = getattr(client, "connection_pool", None)
                if pool is not None:
                    with contextlib.suppress(Exception):
                        await pool.disconnect()

    # ------------------------------------------------------------------ 探针与可观测

    async def ping(self) -> bool:
        try:
            return await self.commands.ping()
        except Exception:
            return False

    def pool_stats(self) -> dict[str, Any]:
        return {
            "service": self.config.service,
            "commands_pool": _pool_usage(self._commands_raw),
            "blocking_pool": _pool_usage(self._blocking_raw),
            "breaker_state": self.breaker.state,
            "telemetry": {
                "total_commands": self.telemetry.stats.total_commands,
                "errors": self.telemetry.stats.errors,
                "slow_commands": self.telemetry.stats.slow_commands,
                "degraded": self.telemetry.stats.degraded,
            },
            "pubsub": {
                "connected": self.pubsub.connected,
                "active_channels": self.pubsub.active_channels,
                "active_subscribers": self.pubsub.active_subscribers,
                "reconnect_count": self.pubsub.reconnect_count,
            },
        }

    # ------------------------------------------------------------------ 高阶原语工厂

    def keyspace(self, domain: str, *, prefix: str = "atlas") -> KeySpace:
        return KeySpace(service=self.config.service, domain=domain, prefix=prefix)

    def lock(self, key: str, *, ttl_s: float = 30.0, owner: str | None = None) -> RedisLock:
        """构造分布式锁：复用独立的 atlas_redislock，并让拿锁/续租/释放都经过 commands 拦截链。"""
        return RedisLock(self.commands, key, ttl_s=ttl_s, owner=owner)  # type: ignore[arg-type]

    def stream(
        self,
        *,
        maxlen: int = 10_000,
        ttl_s: int = 86_400,
        seq_ttl_s: int = 30 * 86_400,
        page_size: int = 500,
    ) -> StreamChannel:
        return StreamChannel(
            commands=self.commands,
            blocking=self.blocking,
            scripts=self.scripts,
            maxlen=maxlen,
            ttl_s=ttl_s,
            seq_ttl_s=seq_ttl_s,
            page_size=page_size,
        )

    def quota(self, *, fail_open: bool = True) -> AtomicQuota:
        return AtomicQuota(commands=self.commands, scripts=self.scripts, fail_open=fail_open)

    def cache(
        self,
        *,
        ttl_s: float = 5.0,
        stale_ttl_s: float = 60.0,
        max_entries: int = 10_000,
        invalidate_channel: str | None = None,
    ) -> CoalescedCache[Any]:
        return CoalescedCache(
            ttl_s=ttl_s,
            stale_ttl_s=stale_ttl_s,
            max_entries=max_entries,
            pubsub=self.pubsub if invalidate_channel else None,
            invalidate_channel=invalidate_channel,
        )
