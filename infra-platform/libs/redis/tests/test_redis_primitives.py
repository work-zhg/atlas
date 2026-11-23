"""高阶原语单测：ScriptRegistry、AtomicQuota、StreamChannel、CoalescedCache、PubSubHub。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import Any

import pytest
from atlas_redis import (
    AtomicQuota,
    CircuitBreaker,
    CoalescedCache,
    PubSubHub,
    RedisClient,
    ResilienceMiddleware,
    Resubscribed,
    ScriptRegistry,
    StreamChannel,
)
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoScriptError

# ───────────────────────────── ScriptRegistry


class _ScriptFakeRaw:
    def __init__(self) -> None:
        self.loaded: dict[str, str] = {}
        self.evalsha_calls = 0
        self.eval_calls = 0

    async def evalsha(self, sha: str, numkeys: int, *args: Any) -> Any:
        self.evalsha_calls += 1
        if sha not in self.loaded:
            raise NoScriptError("NOSCRIPT No matching script")
        return ("sha", sha, numkeys, args)

    async def eval(self, source: str, numkeys: int, *args: Any) -> Any:
        self.eval_calls += 1
        import hashlib

        sha = hashlib.sha1(source.encode()).hexdigest()
        self.loaded[sha] = source
        return ("eval", sha, numkeys, args)


async def test_script_registry_falls_back_on_noscript_then_uses_evalsha() -> None:
    raw = _ScriptFakeRaw()
    client = RedisClient(raw, service="agent")
    reg = ScriptRegistry(client)
    handle = reg.register("demo.inc", "return redis.call('INCR', KEYS[1])")

    # 同名同内容幂等；同名不同内容报错
    assert reg.register("demo.inc", "return redis.call('INCR', KEYS[1])") == handle
    with pytest.raises(ValueError):
        reg.register("demo.inc", "return 42")

    # 首次 EVALSHA 报 NOSCRIPT → 自动回退 EVAL 并由服务端缓存
    first = await reg.run("demo.inc", keys=("k1",), args=("1",))
    assert first[0] == "eval"
    assert raw.evalsha_calls == 1 and raw.eval_calls == 1

    # 第二次直接命中 EVALSHA，零源码传输
    second = await reg.run("demo.inc", keys=("k1",), args=("2",))
    assert second[0] == "sha"
    assert raw.evalsha_calls == 2 and raw.eval_calls == 1


# ───────────────────────────── AtomicQuota


class _QuotaFakeRaw:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.spends: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        self.ttls: dict[str, int] = {}
        self.fail_next = False

    async def evalsha(self, _sha: str, numkeys: int, *args: Any) -> Any:
        if self.fail_next:
            raise RedisConnectionError("redis down")
        key = str(args[0])
        rest = args[numkeys:]
        if len(rest) == 1:
            # fixed_window
            window = int(rest[0])
            self.counts[key] += 1
            if self.counts[key] == 1:
                self.ttls[key] = window
            return [self.counts[key], self.ttls[key]]
        # add_spend
        amount = Decimal(str(rest[0]))
        self.spends[key] += amount
        self.ttls[key] = int(rest[1])
        return str(self.spends[key])

    async def eval(self, _src: str, numkeys: int, *args: Any) -> Any:
        return await self.evalsha("", numkeys, *args)


async def test_atomic_quota_limits_and_degrades_fail_open() -> None:
    raw = _QuotaFakeRaw()
    breaker = CircuitBreaker(service="modelgw", failure_threshold=5)
    client = RedisClient(
        raw, service="modelgw", middlewares=(ResilienceMiddleware(breaker, retry_backoff_s=0.0),)
    )
    quota = AtomicQuota(commands=client, scripts=ScriptRegistry(client), fail_open=True)

    r1 = await quota.allow("k:rpm:1", limit=2, window_s=60)
    r2 = await quota.allow("k:rpm:1", limit=2, window_s=60)
    r3 = await quota.allow("k:rpm:1", limit=2, window_s=60)
    assert (r1.allowed, r2.allowed, r3.allowed) == (True, True, False)
    assert r3.retry_after_s == 60
    assert raw.ttls["k:rpm:1"] == 60

    total = await quota.add_spend("k:spend:1", Decimal("1.25"))
    assert total == Decimal("1.25")

    # Redis 故障时 Fail-Open 降级放行，不抛异常阻塞模型调用
    raw.fail_next = True
    degraded = await quota.allow("k:rpm:1", limit=2, window_s=60)
    assert degraded.allowed is True and degraded.degraded is True
    assert await quota.add_spend("k:spend:1", Decimal("0.5")) is None


# ───────────────────────────── StreamChannel


class _StreamFakeRaw:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.xrange_calls = 0
        self.xrevrange_calls = 0
        self.xread_batches: list[list[Any]] = []
        self.seq = 0

    async def evalsha(self, _sha: str, _numkeys: int, *args: Any) -> int:
        floor = int(args[1])
        if self.seq == 0:
            self.seq = floor
        self.seq += 1
        return self.seq

    def pipeline(self, transaction: bool = False) -> _StreamPipe:
        return _StreamPipe(self)

    async def xrevrange(self, _key: str, *, max: str = "+", min: str = "-", count: int = 1) -> list:
        self.xrevrange_calls += 1
        if not self.entries:
            return []
        return [self.entries[-1]]

    async def xrange(
        self, _key: str, *, min: str = "-", max: str = "+", count: int | None = None
    ) -> list:
        self.xrange_calls += 1
        if min == "-":
            items = self.entries
        elif min.startswith("("):
            after = min[1:]
            idx = next((i for i, (eid, _) in enumerate(self.entries) if eid == after), -1)
            items = self.entries[idx + 1 :]
        else:
            items = self.entries
        return items[:count] if count is not None else items

    async def xread(self, streams: dict[str, str], *, count: int = 100, block: int = 1000) -> list:
        if self.xread_batches:
            return self.xread_batches.pop(0)
        await asyncio.sleep(0)
        return []


class _StreamPipe:
    def __init__(self, raw: _StreamFakeRaw) -> None:
        self._raw = raw
        self._added: str = ""

    async def __aenter__(self) -> _StreamPipe:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def xadd(self, _key: str, fields: dict[str, Any], **_kw: Any) -> _StreamPipe:
        eid = f"{len(self._raw.entries) + 1}-0"
        self._raw.entries.append((eid, {k: str(v) for k, v in fields.items()}))
        self._added = eid
        return self

    def expire(self, _key: str, _ttl: int) -> _StreamPipe:
        return self

    async def execute(self) -> list[Any]:
        return [self._added, True]


async def test_stream_channel_fast_skip_at_tip_and_bad_frame_tolerance() -> None:
    cmd_raw = _StreamFakeRaw()
    blk_raw = _StreamFakeRaw()
    cmd_client = RedisClient(cmd_raw, service="agent")
    blk_client = RedisClient(blk_raw, service="agent")
    ch = StreamChannel(
        commands=cmd_client,
        blocking=blk_client,
        scripts=ScriptRegistry(cmd_client),
        page_size=2,
    )

    # 发布 5 条消息，其中第 4 条故意放坏载荷
    for i in range(1, 6):
        seq = await ch.next_seq("seq:t1", floor=0)
        assert seq == i
        await ch.publish(
            "stream:t1", {"thread_seq": str(seq), "payload": "BAD" if i == 4 else f"ok-{i}"}
        )

    # 1) 末尾快跳：after_seq=5 时只查 1 次 XREVRANGE，0 次 XRANGE！
    blk_raw.xread_batches = [[("stream:t1", [("6-0", {"thread_seq": "6", "payload": "ok-6"})])]]

    def decode(fields: dict[str, Any]) -> str:
        if fields["payload"] == "BAD":
            raise ValueError("corrupt")
        return str(fields["payload"])

    got_tip: list[str] = []
    async for item in ch.tail("stream:t1", after_seq=5, block_ms=10, decode=decode):
        if item is not None:
            got_tip.append(item)
            break
    assert got_tip == ["ok-6"]
    assert cmd_raw.xrevrange_calls == 1
    assert cmd_raw.xrange_calls == 0

    # 2) 分页回放 + 坏帧容错：after_seq=1 跳过 BAD(4)，产出 2, 3, 5，再进 XREAD
    cmd_raw.xrange_calls = 0
    blk_raw.xread_batches = []
    replayed: list[str | None] = []
    async for item in ch.tail("stream:t1", after_seq=1, block_ms=10, decode=decode):
        replayed.append(item)
        if item is None:
            break
    assert replayed == ["ok-2", "ok-3", "ok-5", None]
    assert cmd_raw.xrange_calls == 3  # 5 条按 page_size=2 分 3 页（2, 2, 1）


# ───────────────────────────── PubSubHub


class _FakePubSubConn:
    def __init__(self, script: list[dict[str, Any] | Exception]) -> None:
        self.script = list(script)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.unsubscribed.extend(channels)

    async def get_message(
        self, *, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> Any:
        if not self.script:
            await asyncio.sleep(0.02)
            return None
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def aclose(self) -> None:
        self.closed = True


async def test_pubsub_hub_multiplexes_and_emits_resubscribed_after_reconnect() -> None:
    conns: list[_FakePubSubConn] = [
        _FakePubSubConn(
            [
                {"type": "message", "channel": "ch:1", "data": "hello"},
                RedisConnectionError("drop"),
            ]
        ),
        _FakePubSubConn(
            [
                {"type": "message", "channel": "ch:1", "data": "after-reconnect"},
            ]
        ),
    ]
    idx = 0

    def factory() -> _FakePubSubConn:
        nonlocal idx
        conn = conns[min(idx, len(conns) - 1)]
        idx += 1
        return conn

    pub_raw = _ScriptFakeRaw()  # 只需要一个占位 raw
    hub = PubSubHub(
        commands=RedisClient(pub_raw, service="agent"),
        pubsub_factory=factory,
        backoff_min_s=0.01,
        backoff_max_s=0.02,
    )

    async with hub.subscribe("ch:1") as q1, hub.subscribe("ch:1") as q2:
        # 两个订阅者共享 1 条连接、仅发 1 次 SUBSCRIBE
        assert await asyncio.wait_for(q1.get(), timeout=1.0) == "hello"
        assert await asyncio.wait_for(q2.get(), timeout=1.0) == "hello"
        assert conns[0].subscribed == ["ch:1"]

        # 断线重连后两个订阅者都收到 Resubscribed 哨兵，然后收到新消息
        s1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        s2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert s1 == Resubscribed(channel="ch:1")
        assert s2 == Resubscribed(channel="ch:1")
        assert await asyncio.wait_for(q1.get(), timeout=1.0) == "after-reconnect"
        assert hub.reconnect_count == 1
        assert conns[1].subscribed == ["ch:1"]

    await hub.stop()
    assert hub.active_channels == 0


# ───────────────────────────── CoalescedCache


async def test_coalesced_cache_singleflight_and_stale_fallback() -> None:
    now = 100.0
    cache: CoalescedCache[str] = CoalescedCache(ttl_s=5.0, stale_ttl_s=30.0, clock=lambda: now)
    load_calls = 0
    gate = asyncio.Event()

    async def slow_loader() -> str:
        nonlocal load_calls
        load_calls += 1
        await gate.wait()
        return f"v{load_calls}"

    # 10 个并发 miss 合并为 1 次回源
    tasks = [asyncio.create_task(cache.get_or_load("tenant:1", slow_loader)) for _ in range(10)]
    await asyncio.sleep(0.01)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results == ["v1"] * 10
    assert load_calls == 1

    # TTL 过期后回源 DB 抛异常：在 stale_ttl_s 宽限期内降级返回旧值 v1，不抛 500
    now += 6.0

    async def broken_loader() -> str:
        raise RuntimeError("pg down")

    assert await cache.get_or_load("tenant:1", broken_loader) == "v1"

    # 超过 stale_ttl_s 宽限期后才真实抛错
    now += 30.0
    with pytest.raises(RuntimeError, match="pg down"):
        await cache.get_or_load("tenant:1", broken_loader)


async def test_coalesced_cache_invalidates_on_pubsub_and_resubscribed() -> None:
    conns: list[_FakePubSubConn] = [
        _FakePubSubConn(
            [
                {"type": "message", "channel": "inv:tenant", "data": "t1"},
                RedisConnectionError("drop"),
            ]
        ),
        _FakePubSubConn([]),
    ]
    idx = 0

    def factory() -> _FakePubSubConn:
        nonlocal idx
        c = conns[min(idx, len(conns) - 1)]
        idx += 1
        return c

    hub = PubSubHub(
        commands=RedisClient(_ScriptFakeRaw(), service="modelgw"),
        pubsub_factory=factory,
        backoff_min_s=0.01,
        backoff_max_s=0.02,
    )
    cache: CoalescedCache[bool] = CoalescedCache(
        ttl_s=60.0, stale_ttl_s=120.0, pubsub=hub, invalidate_channel="inv:tenant"
    )
    assert await cache.get_or_load("t1", lambda: _ret(True)) is True
    assert await cache.get_or_load("t2", lambda: _ret(True)) is True
    assert len(cache) == 2

    cache.start_invalidation_listener()
    # 等待收到 t1 驱逐消息及随后重连触发的 Resubscribed 全量清空
    for _ in range(50):
        if len(cache) == 0 and hub.reconnect_count >= 1:
            break
        await asyncio.sleep(0.02)
    assert len(cache) == 0

    await cache.stop_invalidation_listener()
    await hub.stop()


async def _ret(val: Any) -> Any:
    return val
