"""★ P4 完成标准（文档 §16）：30 轮带工具调用的历史强制触发压缩。

§7.3 坑 1（tool_use / tool_result 被切断导致 API 400）在短对话里**永远
暴露不出来** —— 没有工具调用就没有配对可切断。只能靠这个测试兜住。

`test_compaction.py` 测的是切分算法本身（纯函数）；这里测的是接线：
真实的 kernel 图、真实的中间件、真实的 RemoveMessage 状态改写。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from atlas_agent.config import Settings
from atlas_engine.contracts import ContextOverflow
from atlas_engine.contracts.events import EventType
from atlas_engine.contracts.spec import AgentSpec, CompactionSpec, LimitSpec, ModelSpec
from atlas_engine.kernel.context import SUMMARY_PREFIX, ContextMiddleware, estimate_tokens
from atlas_engine.kernel.context._split import would_split_tool_pair
from atlas_engine.kernel.context.compactor import facts_from
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from tests.graphs import context_pipeline
from tests.graphs import run_agent as run

from .fakes import TurnModel

RUN_ID = UUID("88888888-8888-8888-8888-888888888888")

#: 每条工具结果的体量 —— 撑起 token 数，模拟真实的 SQL/检索返回
_BULK = "数据行" * 200

#: 30 轮历史实测约 8400 tokens。窗口取 8000 → 阈值 6000，必然触发。
#: 写成常量而不是散在各处的魔法数：改 _BULK 时只需重算这一处。
_TIGHT_WINDOW = 8_000


def build_history(rounds: int = 30) -> list[BaseMessage]:
    """构造 rounds 个完整回合，每回合含一次工具往返。

    每回合 4 条：
        human(新回合) → ai(tool_use) → tool(tool_result) → ai(正文)
    """
    out: list[BaseMessage] = []
    for i in range(rounds):
        call_id = f"call_{i}"
        out.append(HumanMessage(content=f"第 {i} 个问题：{_BULK[:80]}", id=f"h{i}"))
        out.append(
            AIMessage(
                content="",
                id=f"a{i}",
                tool_calls=[{"name": "lookup", "args": {"q": str(i)}, "id": call_id}],
            )
        )
        out.append(ToolMessage(content=f"{_BULK}", tool_call_id=call_id, name="lookup", id=f"t{i}"))
        out.append(AIMessage(content=f"第 {i} 轮的结论。", id=f"r{i}"))
    return out


class RecordingSummarizer:
    def __init__(self, text: str = "早期对话摘要：用户在做数据核对。") -> None:
        self.text = text
        self.calls: list[list[BaseMessage]] = []

    async def __call__(self, messages: list[BaseMessage]) -> str:
        self.calls.append(list(messages))
        return self.text


def _spec(**over: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "slug": "long",
        "name": "长会话",
        "system_prompt": "p",
        "model": ModelSpec(model="claude-sonnet-5"),
        "limits": LimitSpec(timeout_s=60),
        "compaction": CompactionSpec(keep_recent_turns=3),
    }
    base.update(over)
    return AgentSpec(**base)


# ---------------------------------------------------------------- 切分安全性


def test_naive_split_would_break_pairs() -> None:
    """★ 先证明危险确实存在 —— 否则下面的断言证明不了什么。

    朴素地"按条数砍一半"会落在 tool_use 与 tool_result 之间。
    """
    facts = facts_from(build_history(30))
    broken = [i for i in range(1, len(facts)) if would_split_tool_pair(facts, i)]
    assert broken, "构造的历史里没有可被切断的配对，这个测试就没有意义"


def _rewritten(result: dict[str, Any]) -> list[Any]:
    """压缩写回 state 的形态：REMOVE_ALL + 按序重写的完整列表。"""
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    msgs = result["messages"]
    assert msgs[0].type == "remove" and msgs[0].id == REMOVE_ALL_MESSAGES
    return msgs[1:]


async def test_compaction_never_orphans_a_tool_result() -> None:
    """★ 坑 1 的正面断言：压缩后不存在孤立的 tool_result / 未回应的 tool_use。"""
    history = build_history(30)
    summarizer = RecordingSummarizer()

    captured: dict[str, Any] = {}

    class Spy(ContextMiddleware):
        async def abefore_model(self, state: Any, runtime: Any = None) -> Any:
            result = await super().abefore_model(state, runtime)
            if result is not None:
                captured["result"] = result
                captured["before"] = list(state.get("messages") or [])
            return result

    middleware = context_pipeline(
        cls=Spy,
        context_window=_TIGHT_WINDOW,
        trigger_ratio=0.75,
        keep_recent_turns=3,
        summarizer=summarizer,
    )

    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=TurnModel(scripts=[[AIMessageChunk(content="好的。")]]),
            input_content="继续",
            history=history,
            context=middleware,
        )
    ]

    assert "result" in captured, "30 轮历史没有触发压缩"

    kept = _rewritten(captured["result"])[1:]  # 去掉开头的摘要

    issued: set[str] = set()
    answered: set[str] = set()
    for message in kept:
        for call in getattr(message, "tool_calls", None) or []:
            issued.add(str(call["id"]))
        if call_id := getattr(message, "tool_call_id", None):
            answered.add(str(call_id))

    assert not (answered - issued), f"保留区里有孤立的 tool_result：{answered - issued}"
    assert EventType.RUN_FAILED not in [e.type for e in events], next(
        (e.data for e in events if e.type is EventType.RUN_FAILED), None
    )


async def test_keeps_requested_recent_turns() -> None:
    """保留区确实是最近 N 个完整回合。"""
    history = build_history(30)
    summarizer = RecordingSummarizer()
    middleware = context_pipeline(
        context_window=_TIGHT_WINDOW, trigger_ratio=0.75, keep_recent_turns=3, summarizer=summarizer
    )

    result = await middleware.abefore_model({"messages": history})
    assert result is not None

    rewritten = _rewritten(result)
    # ★ 摘要必须在最前面：只删压缩区再追加摘要的话，reducer 会把摘要放到末尾
    assert rewritten[0].type == "ai" and str(rewritten[0].content).startswith(SUMMARY_PREFIX)
    kept = rewritten[1:]
    starts = [m for m in kept if m.type == "human" and not getattr(m, "tool_call_id", None)]
    assert len(starts) == 3, f"应保留 3 个回合，实际 {len(starts)}"


# ---------------------------------------------------------------- 事件与降级


async def test_emits_context_compacted_event() -> None:
    """★ §7.5：压缩必须对用户可见，否则就是「agent 忘了我说的话」这种无法解释的 bug。"""
    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=TurnModel(scripts=[[AIMessageChunk(content="好的。")]]),
            input_content="继续",
            history=build_history(30),
            context=context_pipeline(
                context_window=_TIGHT_WINDOW,
                trigger_ratio=0.75,
                keep_recent_turns=3,
                summarizer=RecordingSummarizer(),
            ),
        )
    ]

    compacted = [e for e in events if e.type is EventType.CONTEXT_COMPACTED]
    assert compacted, "没有产出 context.compacted 事件"

    data = compacted[0].data
    assert data["messages_summarized"] > 0
    assert data["tokens_after"] < data["tokens_before"], "压缩后 token 没有下降"
    assert data["degraded"] is False
    assert "摘要" in data["summary"]


async def test_compaction_does_not_replay_kept_tool_calls() -> None:
    """压缩整段写回历史（摘要 + 保留区）。保留区里的工具调用早就发过事件 ——
    这一轮模型只回了一句话，不该冒出任何 tool.started（test_event_mapping 的
    test_patched_history_is_not_replayed 是同一个问题的另一个入口）。
    """
    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=TurnModel(scripts=[[AIMessageChunk(content="好的。")]]),
            input_content="继续",
            history=build_history(30),
            context=context_pipeline(
                context_window=_TIGHT_WINDOW,
                trigger_ratio=0.75,
                keep_recent_turns=3,
                summarizer=RecordingSummarizer(),
            ),
        )
    ]

    assert any(e.type is EventType.CONTEXT_COMPACTED for e in events), "没有触发压缩"
    assert [e.data for e in events if e.type is EventType.TOOL_STARTED] == []


async def test_summary_failure_degrades_but_continues() -> None:
    """§7.6：摘要失败降级为丢弃早期消息，标记 degraded，run 继续。"""

    async def broken(_messages: list[BaseMessage]) -> str:
        raise RuntimeError("haiku 挂了")

    events = [
        e
        async for e in run(
            _spec(),
            run_id=RUN_ID,
            model=TurnModel(scripts=[[AIMessageChunk(content="好的。")]]),
            input_content="继续",
            history=build_history(30),
            context=context_pipeline(
                context_window=_TIGHT_WINDOW,
                trigger_ratio=0.75,
                keep_recent_turns=3,
                summarizer=broken,
            ),
        )
    ]
    types = [e.type for e in events]

    compacted = next(e for e in events if e.type is EventType.CONTEXT_COMPACTED)
    assert compacted.data["degraded"] is True
    assert types[-1] is EventType.RUN_FINISHED, "摘要失败把整轮弄挂了"


async def test_thinking_blocks_excluded_from_summary() -> None:
    """§7.3 坑 3：压缩区内的 thinking 不送去摘要。"""
    history = build_history(30)
    history.insert(
        4, AIMessage(content=[{"type": "thinking", "thinking": "内部推理"}], id="think1")
    )

    summarizer = RecordingSummarizer()
    middleware = context_pipeline(
        context_window=_TIGHT_WINDOW, trigger_ratio=0.75, keep_recent_turns=3, summarizer=summarizer
    )
    await middleware.abefore_model({"messages": history})

    sent = summarizer.calls[0]
    assert not any(m.id == "think1" for m in sent), "thinking 被送去摘要了"


# ---------------------------------------------------------------- 不触发的情形


async def test_short_history_is_untouched() -> None:
    """没到阈值就不该动 —— 每轮都压缩会全量击穿 prompt cache（§7.3 坑 2）。"""
    middleware = context_pipeline(
        context_window=1_000_000,
        trigger_ratio=0.75,
        keep_recent_turns=3,
        summarizer=RecordingSummarizer(),
    )
    assert await middleware.abefore_model({"messages": build_history(2)}) is None


async def test_disabled_compaction_leaves_only_offload_and_reminders() -> None:
    """enabled=False：不按档位清理、压缩（连窗口都不去查），外置和提醒照常。

    ★ 外置和提醒不是压缩：关压缩不该把大结果保护和记忆提醒一起关掉。
    """
    from atlas_agent.executor.context_manager import ContextManager

    class _Thread:
        created_by_uuid = None

    class _Segment:
        spec = _spec(compaction=CompactionSpec(enabled=False))
        thread = _Thread()

    from atlas_engine.runtime.context import ContextPorts
    from atlas_engine.runtime.context import context_pipeline as pipeline_for

    settings = Settings()
    manager = ContextManager(None, settings, None)  # type: ignore[arg-type]
    # 关了压缩连窗口都不去查（sessionmaker 是 None，查了就会炸）
    assert await manager._limits_for(_Segment(), workspace=None) is None  # type: ignore[arg-type]
    middleware = pipeline_for(
        _Segment.spec,
        ContextPorts(
            offload_store=None,
            result_limit_tokens=settings.context_result_limit_tokens,
            batch_cap_tokens=settings.context_batch_cap_tokens,
        ),
    )
    assert middleware._budget is None and middleware._compactor is None
    assert middleware._offloader is not None
    assert await middleware.abefore_model({"messages": build_history(30)}) is None


def _limits_segment(model: str, entry: Any = None) -> Any:
    from atlas_agent.executor.run_models import RunModels
    from atlas_agent.providers.llm.model_platform import ModelEndpoint

    class _Segment:
        spec = _spec(model=ModelSpec(model=model, max_output_tokens=16_384))
        models = RunModels(ModelEndpoint("http://gw", "k"), {model: entry} if entry else {})

    return _Segment()


async def test_unregistered_window_falls_back_instead_of_disabling_compaction() -> None:
    """会话 01a1f830：MiniMax-M3 没登记窗口 → 清理、压缩整套静默不装，上下文涨到 220k。

    现在按兜底窗口照装，并标出窗口是估的（用量条据此提示）。
    """
    from atlas_agent.db.session import get_sessionmaker
    from atlas_agent.executor.context_manager import ContextManager

    settings = Settings(context_window_fallback=100_000)
    manager = ContextManager(get_sessionmaker(), settings, None)  # type: ignore[arg-type]
    limits = await manager._limits(_limits_segment("no-such-model"))  # type: ignore[arg-type]
    assert limits == (100_000, 16_384, True)


async def test_registered_window_without_max_output_is_used() -> None:
    """平台只登记了窗口、没登记最大输出：窗口照用，输出上限取智能体配的。"""
    from atlas_agent.executor.context_manager import ContextManager
    from atlas_agent.providers.llm.model_platform import PlatformModel

    entry = PlatformModel("MiniMax-M3", "MiniMax-M3", "minimax", max_input_tokens=1_000_000)
    manager = ContextManager(None, Settings(), None)  # type: ignore[arg-type]
    limits = await manager._limits(_limits_segment("MiniMax-M3", entry))  # type: ignore[arg-type]
    assert limits == (1_000_000, 16_384, False)


def test_assumed_window_is_reported_with_the_usage() -> None:
    from atlas_engine.runtime.context import ContextLimits, ContextPorts
    from atlas_engine.runtime.context import context_pipeline as pipeline_for

    ports = ContextPorts(
        offload_store=None,
        result_limit_tokens=32_000,
        batch_cap_tokens=80_000,
        limits=ContextLimits(
            window=128_000, model_max_output=16_384, summarizer=None, window_assumed=True
        ),
    )
    middleware = pipeline_for(_spec(), ports)
    assert middleware._budget is not None and middleware._budget.window_assumed
    assert middleware._compactor is not None


def _searches(name: str, n: int) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for i in range(n):
        out.append(AIMessage(content="", tool_calls=[{"id": f"s{i}", "name": name, "args": {}}]))
        out.append(ToolMessage(content="结果" * 500, tool_call_id=f"s{i}", id=f"r{i}"))
    return [*out, AIMessage(content="好的")]


def test_read_only_external_tools_are_cleared_like_builtin_ones() -> None:
    """会话 01a1f830：3 次谷歌搜索的原始结果占了 171k 里的三分之一，清理清单里没有 MCP 工具，
    从头跟到尾。只读的外部工具重新调一次就能拿回结果，和 execute、read_file 一样可以清。"""
    from atlas_engine.runtime.context import ContextLimits, ContextPorts
    from atlas_engine.runtime.context import context_pipeline as pipeline_for

    ports = ContextPorts(
        offload_store=None,
        result_limit_tokens=32_000,
        batch_cap_tokens=80_000,
        limits=ContextLimits(window=1_000_000, model_max_output=16_384, summarizer=None),
    )
    middleware = pipeline_for(_spec(), ports, regenerable={"serpapi__google_search"})
    assert middleware._clearing is not None
    cleared = middleware._clearing.plan(_searches("serpapi__google_search", 7))
    assert [m.id for m in cleared] == ["r0", "r1"]  # 最近 5 条留着
    assert "重新调用 serpapi__google_search" in str(cleared[0].content)
    # 没声明只读的外部工具（可能有副作用）照旧不清
    assert middleware._clearing.plan(_searches("crm__create_ticket", 7)) == []


def test_assembly_treats_read_only_tools_as_regenerable(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlas_engine.contracts.tools import ToolSpec
    from atlas_engine.runtime import assemble
    from atlas_engine.runtime.context import ContextPorts

    async def call(**_: Any) -> str:
        return ""

    def spec(name: str, **annotations: Any) -> ToolSpec:
        return ToolSpec(name, "d", {"type": "object"}, call, annotations=annotations)

    seen: dict[str, Any] = {}
    real = assemble.context_pipeline

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(assemble, "context_pipeline", spy)
    assemble.assemble(
        _spec(),
        assemble.Capabilities(
            model=TurnModel(scripts=[]),
            tools=[spec("serpapi__google_search", readOnlyHint=True), spec("crm__create")],
            deferred_tools=[spec("wiki__read", readOnlyHint=True)],
            context=ContextPorts(offload_store=None, result_limit_tokens=1, batch_cap_tokens=1),
        ),
    )
    assert seen["regenerable"] == {"serpapi__google_search", "wiki__read"}


async def test_overflow_when_recent_turns_alone_exceed_window() -> None:
    """§7.6：保留区本身就装不下 → 报错而不是无限压缩。"""
    middleware = context_pipeline(
        context_window=200,  # 比任何一个回合都小
        trigger_ratio=0.75,
        keep_recent_turns=3,
        summarizer=RecordingSummarizer(),
    )
    with pytest.raises(ContextOverflow):
        await middleware.abefore_model({"messages": build_history(2)})


# ---------------------------------------------------------------- 估算


def test_estimate_counts_tool_payloads() -> None:
    """★ 工具返回往往才是撑爆窗口的大头，不能只数正文。"""
    plain = [AIMessage(content="短")]
    with_tool = [ToolMessage(content=_BULK, tool_call_id="x", name="lookup")]
    assert estimate_tokens(with_tool) > estimate_tokens(plain) * 50


def test_summary_message_is_marked() -> None:
    """摘要以 assistant 消息回填，带前缀让模型知道这不是用户说的话。"""
    assert SUMMARY_PREFIX.startswith("【")
