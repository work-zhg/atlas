"""验证 atlas_agent 接入 atlas_redis：EventRelay 快跳/EVALSHA 与 DecisionChannel 多路复用。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from atlas_agent.approval.channel import DecisionChannel
from atlas_agent.stream.relay import EventRelay
from atlas_engine.contracts.events import EventType, TraceEvent
from atlas_redis import PubSubHub, RedisClient, Resubscribed
from redis.exceptions import ConnectionError as RedisConnectionError


class _FakeRelayRaw:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.seq = 0
        self.evalsha_calls = 0
        self.xrevrange_calls = 0
        self.xrange_calls = 0
        self.xread_batches: list[list[Any]] = []

    async def evalsha(self, _sha: str, _numkeys: int, *args: Any) -> int:
        self.evalsha_calls += 1
        floor = int(args[1])
        if self.seq == 0:
            self.seq = floor
        self.seq += 1
        return self.seq

    def pipeline(self, transaction: bool = False) -> _FakePipe:
        return _FakePipe(self)

    async def xrevrange(self, _key: str, *, max: str = "+", min: str = "-", count: int = 1) -> list:
        self.xrevrange_calls += 1
        return [self.entries[-1]] if self.entries else []

    async def xrange(
        self, _key: str, *, min: str = "-", max: str = "+", count: int | None = None
    ) -> list:
        self.xrange_calls += 1
        if min.startswith("("):
            after = min[1:]
            idx = next((i for i, (eid, _) in enumerate(self.entries) if eid == after), -1)
            items = self.entries[idx + 1 :]
        else:
            items = self.entries
        return items[:count] if count is not None else items

    async def xread(self, _streams: dict[str, str], *, count: int = 100, block: int = 1000) -> list:
        if self.xread_batches:
            return self.xread_batches.pop(0)
        return []


class _FakePipe:
    def __init__(self, raw: _FakeRelayRaw) -> None:
        self._raw = raw

    async def __aenter__(self) -> _FakePipe:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def xadd(self, _key: str, fields: dict[str, Any], **_kw: Any) -> _FakePipe:
        eid = f"{len(self._raw.entries) + 1}-0"
        self._raw.entries.append((eid, {k: str(v) for k, v in fields.items()}))
        return self

    def expire(self, _key: str, _ttl: int) -> _FakePipe:
        return self

    async def execute(self) -> list[Any]:
        return [True, True]


def _make_event(run_id: Any, seq: int) -> TraceEvent:
    return TraceEvent(
        seq=seq,
        thread_seq=0,
        run_id=run_id,
        ts=datetime.now(UTC),
        type=EventType.MESSAGE_DELTA,
        depth=0,
        data={"delta": f"tok-{seq}", "block": 0},
    )


async def test_event_relay_uses_evalsha_fast_skip_and_skips_corrupt_entries() -> None:
    raw = _FakeRelayRaw()
    client = RedisClient(raw, service="agent")
    relay = EventRelay(client)
    thread_id = uuid4()
    run_id = uuid4()

    for i in range(1, 4):
        stamped = await relay.publish(_make_event(run_id, i), thread_id=thread_id, floor=0)
        assert stamped.thread_seq == i
    assert raw.evalsha_calls == 3

    # 1) 末尾快跳：after_seq=3 时只查 1 次 XREVRANGE，0 次 XRANGE
    ev4 = _make_event(run_id, 4).stamped(4)
    raw.xread_batches = [
        [
            (
                "stream",
                [("4-0", {"thread_seq": "4", "payload": ev4.model_dump_json()})],
            )
        ]
    ]
    got = []
    async for ev in relay.tail_thread(thread_id, after_seq=3, block_ms=10):
        if ev is not None:
            got.append(ev)
            break
    assert [e.thread_seq for e in got] == [4]
    assert raw.xrevrange_calls == 1
    assert raw.xrange_calls == 0

    # 2) 损坏条目容错：插入一条坏 JSON，回放跳过它继续产出后续合法事件
    raw.entries.insert(1, ("1-5", {"thread_seq": "2", "payload": "{not-valid-json"}))
    raw.xread_batches = []
    replayed = []
    async for ev in relay.tail_thread(thread_id, after_seq=1, block_ms=10):
        if ev is None:
            break
        replayed.append(ev.thread_seq)
    assert replayed == [2, 3]


class _FakeSubConn:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.subscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *_channels: str) -> None:
        return None

    async def get_message(
        self, *, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> Any:
        if not self._script:
            await asyncio.sleep(0.02)
            return None
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def aclose(self) -> None:
        return None


async def test_decision_channel_multiplexes_via_pubsub_hub_and_wakes_on_resubscribed() -> None:
    approval_id = uuid4()
    ch_name = f"atlas:approval:decided:{approval_id}"
    conns = [
        _FakeSubConn([RedisConnectionError("drop")]),
        _FakeSubConn([{"type": "message", "channel": ch_name, "data": "decided"}]),
    ]
    idx = 0

    def factory() -> _FakeSubConn:
        nonlocal idx
        c = conns[min(idx, len(conns) - 1)]
        idx += 1
        return c

    hub = PubSubHub(
        commands=RedisClient(_FakeRelayRaw(), service="agent"),
        pubsub_factory=factory,
        backoff_min_s=0.01,
        backoff_max_s=0.02,
    )
    channel = DecisionChannel(None, pubsub=hub)

    async with channel.listen(approval_id) as signal:
        # 断线重连后收到 Resubscribed 哨兵立刻返回 True 唤醒回库核对
        assert await signal.wait(1.0) is True
        # 随后收到真实决定信号再次唤醒
        assert await signal.wait(1.0) is True
        assert hub.reconnect_count == 1
        assert isinstance(Resubscribed(ch_name), Resubscribed)

    await hub.stop()
