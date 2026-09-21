"""★ P1 完成标准（文档 §16）：给一个 spec，能拿到
run.started → message.delta* → run.finished —— 且**不碰 DB、不碰网络**。

模型由外部注入，所以这里塞一个假模型即可跑完整条链路。
如果哪天这些测试需要起 Postgres 或联网，说明分层破了。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from atlas_server.domain.events import EventType
from tests.graphs import run_agent as run
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec

from tests.fakes import (
    blocks_model,
    raising_model,
    slow_model,
    text_model,
    usage_model,
)

RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
FIXED_TIME = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


def _clock() -> datetime:
    return FIXED_TIME


class AlwaysCancelled:
    async def is_cancelled(self) -> bool:
        return True


def make_spec(**limit_kwargs: Any) -> AgentSpec:
    return AgentSpec(
        slug="data-analyst",
        name="数据分析师",
        system_prompt="你是一名资深数据分析师。",
        model=ModelSpec(model="claude-opus-5", effort="low"),
        limits=LimitSpec(**limit_kwargs) if limit_kwargs else LimitSpec(),
    )


async def collect(**kwargs: Any) -> list[Any]:
    return [e async for e in run(**kwargs)]


# ---------------------------------------------------------------------------
# 主路径 —— 这条就是 P1 的验收
# ---------------------------------------------------------------------------


async def test_p1_acceptance_event_sequence() -> None:
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=text_model("Hello world"),
        input_content="hi",
        clock=_clock,
    )
    types = [e.type for e in events]

    assert types[0] == EventType.RUN_STARTED
    assert types[-1] == EventType.RUN_FINISHED
    assert types.count(EventType.MESSAGE_DELTA) >= 1
    assert EventType.MESSAGE_COMPLETED in types

    text = "".join(e.data["text"] for e in events if e.type == EventType.MESSAGE_DELTA)
    assert text == "Hello world"

    completed = next(e for e in events if e.type == EventType.MESSAGE_COMPLETED)
    assert completed.data["content"] == [{"type": "text", "text": "Hello world"}]


async def test_seq_is_strictly_increasing_without_gaps() -> None:
    """契约规则 2：seq 从 1 严格递增无空洞 —— 前端靠它去重补齐。"""
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=text_model("abcd"),
        input_content="hi",
        clock=_clock,
    )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.run_id == RUN_ID for e in events)
    assert all(e.ts == FIXED_TIME for e in events)


async def test_run_started_carries_model_config() -> None:
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=text_model(),
        input_content="hi",
        clock=_clock,
    )
    data = events[0].data
    assert data["agent_slug"] == "data-analyst"
    assert data["model"] == "claude-opus-5"
    assert data["effort"] == "low"
    assert data["thinking"] == "adaptive"


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


async def test_usage_event_emitted_with_token_counts() -> None:
    usage = {
        "input_tokens": 82,
        "output_tokens": 372,
        "total_tokens": 454,
        "input_token_details": {"cache_read": 10, "cache_creation": 0},
        "output_token_details": {"reasoning": 42},
    }
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=usage_model("ok", usage),
        input_content="hi",
        clock=_clock,
    )
    u = next(e for e in events if e.type == EventType.USAGE_UPDATED).data
    assert u["input_tokens"] == 82
    assert u["output_tokens"] == 372
    assert u["total_tokens"] == 454
    assert u["cache_read"] == 10
    assert u["thinking_tokens"] == 42


async def test_no_usage_event_when_model_reports_none() -> None:
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=text_model("ok"),
        input_content="hi",
        clock=_clock,
    )
    assert EventType.USAGE_UPDATED not in [e.type for e in events]


# ---------------------------------------------------------------------------
# 失败路径 —— 失败也是事件，不是异常（server 的 SSE 中继只需一条路径）
# ---------------------------------------------------------------------------


async def test_cancellation_yields_run_cancelled() -> None:
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=text_model("ab"),
        input_content="hi",
        cancel=AlwaysCancelled(), clock=_clock,
    )
    assert events[-1].type == EventType.RUN_CANCELLED
    assert EventType.RUN_FINISHED not in [e.type for e in events]


async def test_timeout_yields_run_failed_not_exception() -> None:
    events = await collect(
        spec=make_spec(timeout_s=1),
        run_id=RUN_ID,
        model=slow_model("abcde", delay=0.5, pieces=5),
        input_content="hi",
        clock=_clock,
    )
    last = events[-1]
    assert last.type == EventType.RUN_FAILED
    assert last.data["error_kind"] == "timeout"


async def test_model_exception_becomes_run_failed() -> None:
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=raising_model(RuntimeError("gateway exploded")),
        input_content="hi",
        clock=_clock,
    )
    last = events[-1]
    assert last.type == EventType.RUN_FAILED
    assert last.data["error_kind"] == "model_unavailable"
    assert "gateway exploded" in last.data["message"]


async def test_token_limit_exceeded_fails_after_completion() -> None:
    """token 上限是刹车不是硬墙：调用返回后才知道用量（文档 §4.4）。"""
    usage = {"input_tokens": 10, "output_tokens": 10, "total_tokens": 999_999}
    events = await collect(
        spec=make_spec(max_total_tokens=100),
        run_id=RUN_ID,
        model=usage_model("ok", usage),
        input_content="hi",
        clock=_clock,
    )
    types = [e.type for e in events]
    assert EventType.MESSAGE_COMPLETED in types  # 已产出的内容不丢
    assert types[-1] == EventType.RUN_FAILED
    assert events[-1].data["error_kind"] == "limit_exceeded"


async def test_invalid_spec_raises_before_any_event() -> None:
    """spec 不合法应当在发起模型调用前就炸 —— 不产生半截事件流。"""
    from atlas_engine.contracts import InvalidSpec

    bad = AgentSpec(
        slug="x",
        name="X",
        system_prompt="p",
        model=ModelSpec(model="claude-opus-5", temperature=0.2),  # opus-5 传 temperature 会 400
    )
    with pytest.raises(InvalidSpec):
        await collect(
            spec=bad,
            run_id=RUN_ID,
            model=text_model(),
            input_content="hi",
            clock=_clock,
        )


# ---------------------------------------------------------------------------
# 结构化 content blocks（thinking 与 text 混排）
# ---------------------------------------------------------------------------


async def test_thinking_blocks_are_not_treated_as_visible_text() -> None:
    """经网关时 thinking 文本恒为空，但不能把它当正文吐给用户。"""
    chunks = [
        [{"type": "thinking", "thinking": "", "signature": "abc"}],
        [{"type": "text", "text": "答案是 42"}],
    ]
    events = await collect(
        spec=make_spec(),
        run_id=RUN_ID,
        model=blocks_model(chunks),
        input_content="hi",
        clock=_clock,
    )
    deltas = [e.data["text"] for e in events if e.type == EventType.MESSAGE_DELTA]
    assert deltas == ["答案是 42"]
