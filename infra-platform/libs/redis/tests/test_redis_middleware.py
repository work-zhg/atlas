"""KeySpace、RedisConfig 与中间件拦截器链（Guard / Telemetry / Resilience）单测。"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import pytest
from atlas_redis import (
    FAIL_OPEN_FALLBACK,
    CircuitBreaker,
    CircuitOpen,
    CommandContext,
    CommandForbidden,
    GuardMiddleware,
    KeySpace,
    PayloadTooLarge,
    RedisClient,
    RedisConfig,
    RedisUnavailable,
    ResilienceMiddleware,
    TelemetryMiddleware,
)
from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRawRedis:
    """极简内存假客户端，可编程注入连接故障与延迟。"""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.failures: list[Exception] = []

    def _maybe_fail(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    async def execute_command(self, cmd: str, *args: Any) -> Any:
        self.calls.append((cmd, args))
        self._maybe_fail()
        return "OK"

    async def ping(self) -> bool:
        self.calls.append(("PING", ()))
        self._maybe_fail()
        return True

    async def get(self, key: str) -> Any:
        self.calls.append(("GET", (key,)))
        self._maybe_fail()
        return self.kv.get(key)

    async def set(self, key: str, value: Any, **_kwargs: Any) -> bool:
        self.calls.append(("SET", (key, value)))
        self._maybe_fail()
        self.kv[key] = value
        return True

    async def incr(self, key: str) -> int:
        self.calls.append(("INCR", (key,)))
        self._maybe_fail()
        val = int(self.kv.get(key, 0)) + 1
        self.kv[key] = val
        return val

    async def getset(self, key: str, value: Any) -> Any:
        self.calls.append(("GETSET", (key, value)))
        self._maybe_fail()
        previous = self.kv.get(key)
        self.kv[key] = value
        return previous

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, raw: FakeRawRedis) -> None:
        self._raw = raw
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def set(self, key: str, value: Any, **_kwargs: Any) -> _FakePipeline:
        self._ops.append(("SET", (key, value)))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self._ops.append(("EXPIRE", (key, seconds)))
        return self

    async def execute(self) -> list[Any]:
        self._raw._maybe_fail()
        out = []
        for cmd, args in self._ops:
            self._raw.calls.append((cmd, args))
            if cmd == "SET":
                self._raw.kv[args[0]] = args[1]
                out.append(True)
            else:
                out.append(True)
        return out


# ───────────────────────────── KeySpace & Config


def test_keyspace_format_tenant_and_hash_tag() -> None:
    ks = KeySpace(service="agent", domain="thread")
    tid = UUID("01950000-0000-7000-8000-000000000001")
    assert ks.key("events", tid) == f"atlas:agent:sys:thread:events:{tid}"
    assert ks.key("events", tenant=tid, hash_tag="t:abc") == (
        f"atlas:agent:t:{tid}:thread:{{t:abc}}:events"
    )
    assert ks.pattern("events", "*", has_tenant=True) == "atlas:agent:t:*:thread:events:*"


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"parts": ("bad key",)},
        {"parts": ("bad\nkey",)},
        {"parts": ("ok",), "hash_tag": "a{b}"},
        {"parts": ("ok",), "hash_tag": ""},
    ],
)
def test_keyspace_rejects_invalid_segments(bad_kwargs: dict[str, Any]) -> None:
    ks = KeySpace(service="agent", domain="thread")
    parts = bad_kwargs.pop("parts")
    with pytest.raises(ValueError):
        ks.key(*parts, **bad_kwargs)


def test_config_validation() -> None:
    cfg = RedisConfig(url="redis://localhost:6380/0", service="modelgw")
    assert cfg.command_socket_timeout_s == 2.0
    assert cfg.blocking_socket_timeout_s is None

    with pytest.raises(ValueError):
        RedisConfig(url="", service="agent")
    with pytest.raises(ValueError):
        RedisConfig(url="redis://localhost:6380/0", service="Bad Service!")
    with pytest.raises(ValueError):
        RedisConfig(url="redis://localhost:6380/0", service="agent", command_socket_timeout_s=0)


# ───────────────────────────── GuardMiddleware


async def test_guard_forbids_dangerous_command_and_pipeline_subcommand() -> None:
    raw = FakeRawRedis()
    client = RedisClient(raw, service="agent", middlewares=(GuardMiddleware(),))

    with pytest.raises(CommandForbidden):
        await client.execute("KEYS", "*")
    assert raw.calls == []

    # 管线内夹带危险命令也必须拦截
    guard = GuardMiddleware()
    ctx = CommandContext(
        service="agent",
        command="PIPELINE",
        is_pipeline=True,
        subcommands=("SET", "FLUSHDB"),
    )
    with pytest.raises(CommandForbidden):
        await guard(ctx, lambda _c: _noop())


async def test_guard_payload_warn_and_hard_reject(caplog: pytest.LogCaptureFixture) -> None:
    raw = FakeRawRedis()
    guard = GuardMiddleware(warn_bytes=16, max_bytes=32)
    client = RedisClient(raw, service="agent", middlewares=(guard,))

    with caplog.at_level(logging.WARNING):
        await client.set("k1", "x" * 20, key_pattern="agent:test:*")
    assert "Redis 写入载荷偏大" in caplog.text

    with pytest.raises(PayloadTooLarge):
        await client.set("k2", "x" * 40)
    assert "k2" not in raw.kv


# ───────────────────────────── TelemetryMiddleware


async def test_telemetry_counts_and_slow_log(caplog: pytest.LogCaptureFixture) -> None:
    raw = FakeRawRedis()
    tel = TelemetryMiddleware(slow_ms=0.0, enable_otel=False)
    client = RedisClient(raw, service="agent", middlewares=(tel,))

    with caplog.at_level(logging.WARNING):
        await client.set("secret_key", "super-secret-token", key_pattern="atlas:agent:sys:demo:*")

    assert tel.stats.total_commands == 1
    assert tel.stats.slow_commands == 1
    assert "atlas:agent:sys:demo:*" in caplog.text
    # ★ 慢日志绝不能泄露敏感 value
    assert "super-secret-token" not in caplog.text


# ───────────────────────────── ResilienceMiddleware & CircuitBreaker


async def test_resilience_retries_idempotent_read_but_never_retries_write() -> None:
    raw = FakeRawRedis()
    raw.kv["k"] = "hello"
    breaker = CircuitBreaker(service="agent", failure_threshold=3, recovery_s=10.0)
    client = RedisClient(
        raw,
        service="agent",
        middlewares=(ResilienceMiddleware(breaker, retry_backoff_s=0.0),),
    )

    # 幂等读：首次断连自动重试 1 次成功
    raw.failures = [RedisConnectionError("reset")]
    assert await client.get("k") == "hello"
    assert len(raw.calls) == 2

    # 写命令（INCR）：绝不重试，首次断连立刻抛出 RedisUnavailable
    raw.calls.clear()
    raw.failures = [RedisConnectionError("reset")]
    with pytest.raises(RedisUnavailable):
        await client.incr("counter")
    assert len(raw.calls) == 1


async def test_getset_returns_the_previous_value_and_is_never_retried() -> None:
    """★ GETSET 是写：断连不重试。重试的话，第一次其实写进去了时，拿回的是自己刚写的值。"""
    raw = FakeRawRedis()
    breaker = CircuitBreaker(service="agent", failure_threshold=3, recovery_s=10.0)
    client = RedisClient(
        raw,
        service="agent",
        middlewares=(ResilienceMiddleware(breaker, retry_backoff_s=0.0),),
    )

    assert await client.getset("seen", "a") is None
    assert await client.getset("seen", "b") == "a"
    assert raw.kv["seen"] == "b"

    raw.calls.clear()
    raw.failures = [RedisConnectionError("reset")]
    with pytest.raises(RedisUnavailable):
        await client.getset("seen", "c")
    assert len(raw.calls) == 1


async def test_circuit_breaker_opens_and_fail_open_short_circuits() -> None:
    now = 100.0
    raw = FakeRawRedis()
    breaker = CircuitBreaker(
        service="modelgw", failure_threshold=2, recovery_s=10.0, clock=lambda: now
    )
    tel = TelemetryMiddleware(slow_ms=1000.0, enable_otel=False)
    strict = RedisClient(
        raw,
        service="modelgw",
        middlewares=(tel, ResilienceMiddleware(breaker, retry_backoff_s=0.0)),
    )
    soft = strict.with_policy(fail_open=True)

    # 连续两次写失败打开熔断器
    raw.failures = [RedisConnectionError("down"), RedisConnectionError("down")]
    with pytest.raises(RedisUnavailable):
        await strict.incr("a")
    with pytest.raises(RedisUnavailable):
        await strict.incr("a")
    assert breaker.state == "open"

    # 熔断开启期间：Fail-Closed 立刻抛 CircuitOpen，且不发任何网络调用
    raw.calls.clear()
    with pytest.raises(CircuitOpen):
        await strict.incr("a")
    assert raw.calls == []

    # Fail-Open 视图 0ms 短路降级，同样不发网络调用
    assert await soft.incr("a") is FAIL_OPEN_FALLBACK
    assert raw.calls == []
    assert tel.stats.degraded == 1

    # 经过恢复窗口后进入 half-open，放行 1 个探测请求成功后自动闭合
    now += 10.5
    assert breaker.state == "half_open"
    assert await strict.incr("a") == 1
    assert breaker.state == "closed"


async def _noop() -> None:
    return None
