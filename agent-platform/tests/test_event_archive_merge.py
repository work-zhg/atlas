"""归档时 message.delta 按段合并（EventArchive.archive_events）。纯逻辑，假 session，不碰库。

为什么要归档 delta：正文出现在哪个位置只有它知道。只靠段末的 message.completed 回放，
模型调工具前说的话会被排到工具后面 —— 刷新后过程性发言「掉到下面」。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from atlas_agent.repositories.event_archive import EventArchive
from atlas_engine.contracts.events import EventType, TraceEvent


class _Session:
    """只记下 INSERT 的行。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def execute(self, _stmt: Any, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)


def _ev(
    run: UUID, n: int, type_: EventType, data: dict, *, depth: int = 0, tseq: int | None = None
):
    return TraceEvent(
        seq=n,
        thread_seq=n if tseq is None else tseq,
        run_id=run,
        ts=datetime.now(UTC),
        depth=depth,
        type=type_,
        data=data,
    )


async def _archive(events: list[TraceEvent]) -> list[tuple[int, str, str | None]]:
    session = _Session()
    await EventArchive(session).archive_events(events, thread_id=uuid4())  # type: ignore[arg-type]
    return [(r["thread_seq"], r["type"], r["data"].get("text")) for r in session.rows]


async def test_consecutive_deltas_of_a_block_become_one_row_at_the_first_position() -> None:
    run = uuid4()
    delta = EventType.MESSAGE_DELTA
    rows = await _archive(
        [
            _ev(run, 1, EventType.RUN_STARTED, {}),
            _ev(run, 2, delta, {"text": "我先", "block": 0}),
            _ev(run, 3, delta, {"text": "看看", "block": 0}),
            _ev(run, 4, EventType.TOOL_STARTED, {"call_id": "c1", "name": "ls"}),
            _ev(run, 5, delta, {"text": "结论", "block": 1}),
            _ev(run, 6, delta, {"text": "是 42", "block": 1}),
        ]
    )
    assert rows == [
        (1, "run.started", None),
        (2, "message.delta", "我先看看"),
        (4, "tool.started", None),
        (5, "message.delta", "结论是 42"),
    ]


async def test_only_adjacent_deltas_of_the_same_run_and_block_merge() -> None:
    """夹了别的事件、换了 run、换了段都另起一行 —— 回放时前端按段号接上，结果一样。"""
    run, child = uuid4(), uuid4()
    delta = EventType.MESSAGE_DELTA
    rows = await _archive(
        [
            _ev(run, 1, delta, {"text": "a", "block": 0}),
            _ev(child, 2, delta, {"text": "子", "block": 0}, depth=1),
            _ev(run, 3, delta, {"text": "b", "block": 0}),
            _ev(run, 4, EventType.USAGE_UPDATED, {}),
            _ev(run, 5, delta, {"text": "c", "block": 0}),
            _ev(run, 6, delta, {"text": "d", "block": 1}),
        ]
    )
    texts = [(seq, text) for seq, kind, text in rows if kind == "message.delta"]
    assert texts == [(1, "a"), (2, "子"), (3, "b"), (5, "c"), (6, "d")]


async def test_thinking_and_unstamped_events_are_still_dropped_and_inputs_untouched() -> None:
    run = uuid4()
    events = [
        _ev(run, 1, EventType.MESSAGE_DELTA, {"text": "a", "block": 0}),
        _ev(run, 2, EventType.THINKING_DELTA, {"text": "嗯"}),
        _ev(run, 3, EventType.MESSAGE_DELTA, {"text": "b", "block": 0}),
        _ev(run, 4, EventType.MESSAGE_DELTA, {"text": "没盖序号", "block": 0}, tseq=0),
    ]
    before = [dict(e.data) for e in events]
    assert await _archive(events) == [(1, "message.delta", "ab")]
    # 合并产生新的 data，不改原事件（它们还要发往 Redis / 交给别的消费者）
    assert [dict(e.data) for e in events] == before
