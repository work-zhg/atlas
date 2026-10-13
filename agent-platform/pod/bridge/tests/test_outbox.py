"""Outbox：序号、顺序、确认、换连接（Bridge 设计 §6.6 · §7.3）。补发与淘汰在后续步骤。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from atlas_bridge.testing.harness import until
from atlas_bridge.upstream.outbox import Outbox
from atlas_host import AgentUpdateParams, SessionEndedParams
from atlas_jsonrpc import ChannelClosed


class Sink:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.broken = False
        self.gate: asyncio.Event | None = None

    async def send_text(self, text: str) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.broken:
            raise ChannelClosed("gone")
        self.sent.append(json.loads(text))


def update(outbox: Outbox, text: str = "x") -> int:
    return outbox.put_notification(
        "agent.update",
        lambda seq: AgentUpdateParams(seq=seq, origin="stray", update={"t": text}),
        kind="data",
    )


@pytest.fixture
async def outbox() -> Any:
    box = Outbox()
    pump = asyncio.create_task(box.pump())
    yield box
    pump.cancel()


async def test_seq_is_contiguous_and_messages_are_ready_to_send(outbox: Outbox) -> None:
    assert [update(outbox) for _ in range(3)] == [1, 2, 3]
    ended = outbox.put_notification(
        "session.ended", lambda seq: SessionEndedParams(seq=seq, cause="x"), kind="control"
    )
    assert ended == 4 and outbox.last_seq == 4
    sink = Sink()
    outbox.bind(sink)
    await asyncio.sleep(0.01)
    assert sink.sent == []  # ★ 新连接先暂不发送，直到它发来第一个请求
    outbox.release(sink)
    await until(lambda: len(sink.sent) == 4)
    assert [m["params"]["seq"] for m in sink.sent] == [1, 2, 3, 4]
    assert sink.sent[0] == {
        "jsonrpc": "2.0",
        "method": "agent.update",
        "params": {"seq": 1, "origin": "stray", "update": {"t": "x"}},
    }


async def test_response_is_sent_after_everything_put_before_it(outbox: Outbox) -> None:
    """★ session.open 的响应必须排在重放的历史之后（§4.5）：两者走同一个队列。"""
    sink = Sink()
    sink.gate = asyncio.Event()
    outbox.bind(sink)
    outbox.release(sink)
    update(outbox, "replay-1")
    update(outbox, "replay-2")
    outbox.put_response(sink, '{"jsonrpc":"2.0","id":1,"result":{}}')
    sink.gate.set()
    await until(lambda: len(sink.sent) == 3)
    assert [m.get("id") for m in sink.sent] == [None, None, 1]


async def test_ack_releases_retained_entries(outbox: Outbox) -> None:
    for _ in range(5):
        update(outbox)
    assert outbox.retained == 5
    outbox.ack(3)
    assert outbox.retained == 2 and outbox.acked == 3
    outbox.ack(2)  # 回退的 ack 忽略
    assert outbox.acked == 3
    outbox.ack(99)  # 不超过已分配的序号
    assert outbox.acked == 5 and outbox.retained == 0


async def test_unsent_messages_wait_for_the_next_connection(outbox: Outbox) -> None:
    first = Sink()
    outbox.bind(first)
    outbox.release(first)
    update(outbox)
    await until(lambda: len(first.sent) == 1)
    first.broken = True
    update(outbox)
    await until(lambda: outbox.conn is None)  # 发送失败 → 视为断开
    update(outbox)
    second = Sink()
    outbox.bind(second)
    outbox.release(second)
    await until(lambda: len(second.sent) == 2)
    assert [m["params"]["seq"] for m in second.sent] == [2, 3]


async def test_response_for_an_old_connection_is_dropped(outbox: Outbox) -> None:
    old, new = Sink(), Sink()
    outbox.put_response(old, '{"jsonrpc":"2.0","id":1,"result":{}}')
    update(outbox)
    outbox.bind(new)
    outbox.release(new)
    await until(lambda: len(new.sent) == 1)
    await asyncio.sleep(0)
    assert new.sent[0]["method"] == "agent.update" and old.sent == []


async def test_drained(outbox: Outbox) -> None:
    sink = Sink()
    outbox.bind(sink)
    outbox.release(sink)
    update(outbox)
    await asyncio.wait_for(outbox.drained(), 1)
    assert len(sink.sent) == 1


# ═══════════════════════════════════ 补发、淘汰、背压 ═══════════════════════════════════


async def test_attach_resends_everything_after_last_seq_behind_the_response(outbox: Outbox) -> None:
    """★ 断线前已发出、但 server 没处理完的消息，attach 之后按 seq 重新发一遍（§7.3）。"""
    first = Sink()
    outbox.bind(first)
    outbox.release(first)
    for _ in range(5):
        update(outbox)
    await until(lambda: len(first.sent) == 5)
    outbox.bind(None)  # 断线：server 只处理完了 1..2
    update(outbox)  # 断线期间又来一条（6）

    second = Sink()
    outbox.bind(second)
    resend_from, gaps = outbox.resend_plan(2)
    assert (resend_from, gaps) == (3, [])
    outbox.rewind(second, '{"jsonrpc":"2.0","id":7,"result":{"resendFrom":3}}')
    update(outbox)  # 实时消息（7）排在补发之后
    await until(lambda: len(second.sent) == 6)
    assert second.sent[0]["id"] == 7  # 先是 attach 的响应
    assert [m["params"]["seq"] for m in second.sent[1:]] == [3, 4, 5, 6, 7]
    assert outbox.retained == 5  # 3..7 仍保留，直到被确认


async def test_disconnected_overflow_evicts_oldest_data_and_records_gaps() -> None:
    """★ 断线期间缓冲将满：先淘汰最老的 agent.update，控制消息永不淘汰（§7.4）。"""
    box = Outbox(max_bytes=600)
    update(box, "a" * 100)  # 1
    control = box.put_notification(
        "session.ended", lambda seq: SessionEndedParams(seq=seq, cause="x"), kind="control"
    )  # 2
    for _ in range(6):
        update(box, "b" * 100)  # 3..8
    assert box.bytes_held <= 600
    assert box.gaps and box.gaps[0][0] == 1
    retained = [e.seq for e in box._retained]
    assert control in retained  # 控制消息还在
    resend_from, gaps = box.resend_plan(0)
    assert gaps == box.gaps and resend_from == 2


async def test_connected_overflow_is_backpressure_not_eviction() -> None:
    clock = [0.0]
    box = Outbox(max_bytes=300, clock=lambda: clock[0])
    sink = Sink()
    sink.gate = asyncio.Event()  # 发不出去
    box.bind(sink)
    box.release(sink)
    for _ in range(4):
        update(box, "c" * 100)
    assert box.gaps == [] and box.bytes_held > 300  # 连接在：不淘汰
    waiter = asyncio.create_task(box.wait_writable())
    await asyncio.sleep(0.01)
    assert not waiter.done()  # 背压：读取 agent 的一方停下
    clock[0] = 31.0
    assert box.full_for() == 31.0  # 看门狗据此以 1011 断开
    box.ack(4)  # server 确认了 → 腾出空间
    await asyncio.wait_for(waiter, 1)
    assert box.full_for() == 0.0
    sink.gate.set()


async def test_backpressure_is_released_by_a_disconnect() -> None:
    """连接断开：不再等 server，改为淘汰旧数据，读取 agent 恢复。"""
    box = Outbox(max_bytes=300)
    sink = Sink()
    sink.gate = asyncio.Event()
    box.bind(sink)
    box.release(sink)
    for _ in range(4):
        update(box, "d" * 100)
    waiter = asyncio.create_task(box.wait_writable())
    await asyncio.sleep(0.01)
    box.bind(None)
    await asyncio.wait_for(waiter, 1)
    assert box.bytes_held <= 300 and box.gaps
