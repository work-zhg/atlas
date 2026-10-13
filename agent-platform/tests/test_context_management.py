"""上下文管理（doc/context-management-design.html）：计量、清理、封顶、压缩后工作集、
反应式恢复、熔断、附件、构成事件。"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_engine.kernel.middleware._context import (
    TokenMeter,
    batch_overflow,
    clear_plan,
    is_context_overflow,
    working_set,
)
from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX, CompactionMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

BULK = "数据" * 4000  # ≈ 3200 tokens


def _turn(i: int, tool: str = "read_file", path: str | None = None, body: str = BULK) -> list[Any]:
    cid = f"c{i}"
    return [
        HumanMessage(content=f"问题 {i}", id=f"h{i}"),
        AIMessage(
            content="",
            id=f"a{i}",
            tool_calls=[{"id": cid, "name": tool, "args": {"file_path": path or f"/f{i}.py"}}],
        ),
        ToolMessage(content=body, tool_call_id=cid, name=tool, id=f"t{i}"),
        AIMessage(content=f"回答 {i}", id=f"r{i}"),
    ]


def _history(n: int, **kw: Any) -> list[Any]:
    return [m for i in range(n) for m in _turn(i, **kw)]


async def _summ(messages: Any) -> str:
    return "摘要内容"


def _mw(**kw: Any) -> CompactionMiddleware:
    base: dict[str, Any] = {
        "context_window": 200_000,
        "trigger_ratio": 0.8,
        "keep_recent_turns": 2,
        "summarizer": _summ,
    }
    return CompactionMiddleware(**{**base, **kw})


# ─────────────────────────────────────────────── 纯函数


def test_meter_uses_last_usage_as_baseline() -> None:
    msgs = [
        HumanMessage(content="x" * 1000),
        AIMessage(
            content="y",
            usage_metadata={"input_tokens": 5000, "output_tokens": 100, "total_tokens": 5100},
        ),
        ToolMessage(content="z" * 250, tool_call_id="c"),
    ]
    tokens, basis = TokenMeter().measure(msgs)
    assert basis == "usage" and tokens == 5100 + 100
    tokens2, basis2 = TokenMeter(overhead_tokens=50).measure(msgs[:1])
    assert basis2 == "estimate" and tokens2 == 400 + 50


def test_clear_plan_keeps_recent_and_last_batch() -> None:
    msgs = _history(8)
    plan = clear_plan(msgs, clearable={"read_file"}, keep_recent=3)
    cleared = {m.id for m in plan}
    assert cleared == {"t0", "t1", "t2", "t3", "t4"}  # 8 个里保留最近 3 个
    assert all(m.content.startswith("[旧的工具结果已清除") for m in plan)
    assert all(m.tool_call_id == f"c{m.id[1:]}" for m in plan)  # 配对不变
    # 不可再生的工具不清；最后一批（最后一条 AI 之后）不清
    assert clear_plan(_history(8, tool="write_file"), clearable={"read_file"}, keep_recent=0) == []
    tail = [*_history(2)[:-1]]  # 以 ToolMessage 结尾
    assert "t1" not in {m.id for m in clear_plan(tail, clearable={"read_file"}, keep_recent=0)}


def test_batch_overflow_picks_largest_first() -> None:
    batch = [
        AIMessage(
            content="", tool_calls=[{"id": f"b{i}", "name": "x", "args": {}} for i in range(3)]
        ),
        ToolMessage(content="a" * 25_000, tool_call_id="b0", id="m0"),
        ToolMessage(content="a" * 250_000, tool_call_id="b1", id="m1"),
        ToolMessage(content="a" * 50_000, tool_call_id="b2", id="m2"),
    ]
    assert [m.id for m in batch_overflow(batch, cap_tokens=40_000)] == ["m1"]
    assert batch_overflow(batch, cap_tokens=200_000) == []


def test_working_set_orders_by_recency_and_splits_skills() -> None:
    msgs = [
        *_turn(0, path="/a.py"),
        *_turn(1, path="/skills/pdf/SKILL.md"),
        *_turn(2, path="/b.py"),
        *_turn(3, path="/a.py"),
        *_turn(4, path="/large_tool_results/x.txt"),
    ]
    files, skills = working_set(msgs, max_files=5)
    assert files == ["/a.py", "/b.py"] and skills == ["/skills/pdf/SKILL.md"]


def test_overflow_detection() -> None:
    class E(Exception):
        status_code = 400

    assert is_context_overflow(E("prompt is too long: 210000 tokens > 200000 maximum"))
    assert is_context_overflow(E("This model's maximum context length is 65536 tokens"))
    assert not is_context_overflow(E("invalid api key"))

    class E5(Exception):
        status_code = 500

    assert not is_context_overflow(E5("context length"))


# ─────────────────────────────────────────────── before_model


async def test_clearing_tier_replaces_old_results_in_place() -> None:
    mw = _mw(context_window=60_000, clear_ratio=0.3, trigger_ratio=0.95, keep_recent_tool_results=2)
    out = await mw.abefore_model({"messages": _history(8)})
    assert out is not None
    msgs = out["messages"]
    assert all(m.type == "tool" for m in msgs)  # 按 id 原地替换，不重写整段
    assert {m.id for m in msgs} == {f"t{i}" for i in range(6)}


async def test_idle_trigger_clears_once_even_below_threshold() -> None:
    mw = _mw(clear_ratio=0.9, idle_s=4000, cache_ttl_s=3600, keep_recent_tool_results=1)
    out = await mw.abefore_model({"messages": _history(3)})
    assert out is not None and {m.id for m in out["messages"]} == {"t0", "t1"}
    assert await mw.abefore_model({"messages": _history(3)}) is None  # 只在首次调用时判断


async def test_compaction_restores_working_set_from_filesystem() -> None:
    class FS:
        async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> Any:
            class R:
                def __init__(self) -> None:
                    self.file_data = {"content": f"当前内容 of {path}"}

            return R()

    persisted: list[dict[str, Any]] = []

    async def on_compacted(p: dict[str, Any]) -> None:
        persisted.append(p)

    mw = _mw(context_window=20_000, filesystem=FS(), on_compacted=on_compacted)
    out = await mw.abefore_model({"messages": _history(6)})
    assert out is not None
    msgs = out["messages"]
    assert msgs[0].id == REMOVE_ALL_MESSAGES
    summary = msgs[1]
    assert summary.content.startswith(SUMMARY_PREFIX + "摘要内容")
    assert "当前内容 of /f3.py" in summary.content and "压缩前正在处理的内容" in summary.content
    # 持久化的摘要不含工作集（文件之后会变，下一轮重新读）
    assert persisted[0]["summary"] == "摘要内容" and "/f3.py" in persisted[0]["restored_files"]


async def test_circuit_breaker_stops_after_failures() -> None:
    calls = 0

    async def bad(messages: Any) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("summarizer down")

    failures: list[int] = []

    async def on_failed(n: int) -> None:
        failures.append(n)

    mw = _mw(context_window=20_000, summarizer=bad, failure_count=2, on_failed=on_failed)
    out = await mw.abefore_model({"messages": _history(6)})
    assert out is not None and failures == [3]  # 降级照样压掉，计数到 3
    assert await mw.abefore_model({"messages": _history(6)}) is None  # 熔断：不再主动压缩
    assert calls == 1


# ─────────────────────────────────────────────── wrap_model_call


class _Model:
    max_tokens = 4096


def _req(messages: list[Any], **kw: Any) -> ModelRequest:
    return ModelRequest(model=_Model(), messages=messages, state=kw.pop("state", {}), **kw)  # type: ignore[arg-type]


async def test_overflow_triggers_forced_compaction_and_retry() -> None:
    seen: list[int] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        seen.append(len(req.messages))
        if len(seen) == 1:

            class E(Exception):
                status_code = 400

            raise E("prompt is too long")
        return ModelResponse(result=[AIMessage(content="ok")])

    mw = _mw()
    history = _history(6)
    resp = await mw.awrap_model_call(_req(history), handler)
    assert resp.result[0].content == "ok"
    assert seen[0] == len(history) and seen[1] < len(history)
    # 压缩结果在下一个 before_model 写回 state（含之后新增的消息）
    later = [*history, AIMessage(content="ok", id="new")]
    out = await mw.abefore_model({"messages": later})
    assert out["messages"][0].id == REMOVE_ALL_MESSAGES
    assert out["messages"][1].content.startswith(SUMMARY_PREFIX) and out["messages"][-1].id == "new"


async def test_truncated_output_retries_with_higher_limit() -> None:
    settings: list[dict[str, Any]] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        settings.append(dict(req.model_settings))
        stop = "max_tokens" if len(settings) == 1 else "end_turn"
        return ModelResponse(
            result=[AIMessage(content="x", response_metadata={"stop_reason": stop})]
        )

    mw = _mw(max_output_cap=32_000)
    resp = await mw.awrap_model_call(_req([HumanMessage(content="hi")]), handler)
    assert settings == [{}, {"max_tokens": 8192}]
    assert resp.result[0].response_metadata["stop_reason"] == "end_turn"


async def test_attachments_are_appended_not_persisted() -> None:
    sent: list[Any] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        sent.append(req.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    todos = [{"content": "写测试", "status": "in_progress"}]
    history = [HumanMessage(content="开始"), *[AIMessage(content=f"step{i}") for i in range(9)]]
    mw = _mw(attachments=[("memory", "用户偏好中文")], todo_reminder_after=8)
    await mw.awrap_model_call(_req(history, state={"todos": todos}), handler)
    last = sent[0][-1]
    assert isinstance(last, HumanMessage) and "<system-reminder" in last.content
    assert "用户偏好中文" in last.content and "写测试" in last.content
    assert len(history) == 10  # 原始消息列表没被改


async def test_usage_warning_reminder_near_limit() -> None:
    sent: list[Any] = []

    async def handler(req: ModelRequest) -> ModelResponse:
        sent.append(req.messages)
        return ModelResponse(result=[AIMessage(content="ok")])

    mw = _mw(context_window=10_000, warn_ratio=0.5, trigger_ratio=0.95)
    big = [HumanMessage(content="数据" * 8000)]
    await mw.awrap_model_call(_req(big), handler)
    assert 'name="context_usage"' in sent[0][-1].content


@pytest.mark.parametrize("ratio", [0.8])
def test_effective_window_reserves_output(ratio: float) -> None:
    mw = _mw(context_window=100_000, max_output_tokens=16_384, trigger_ratio=ratio)
    assert mw._effective == 100_000 - 16_384 and mw._trigger == int((100_000 - 16_384) * ratio)
