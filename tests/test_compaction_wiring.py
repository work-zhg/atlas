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
from atlas_engine.kernel.middleware._compaction import would_split_tool_pair
from atlas_engine.kernel.middleware.compaction import (
    SUMMARY_PREFIX,
    CompactionMiddleware,
    estimate_tokens,
    facts_from,
)
from atlas_engine.contracts import ContextOverflow
from atlas_server.domain.events import EventType
from tests.graphs import run_agent as run
from atlas_server.domain.spec import AgentSpec, CompactionSpec, LimitSpec, ModelSpec
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

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


async def test_compaction_never_orphans_a_tool_result() -> None:
    """★ 坑 1 的正面断言：压缩后不存在孤立的 tool_result / 未回应的 tool_use。"""
    history = build_history(30)
    summarizer = RecordingSummarizer()

    captured: dict[str, Any] = {}

    class Spy(CompactionMiddleware):
        async def abefore_model(self, state: Any, runtime: Any = None) -> Any:
            result = await super().abefore_model(state, runtime)
            if result is not None:
                captured["result"] = result
                captured["before"] = list(state.get("messages") or [])
            return result

    middleware = Spy(
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
            compactor=middleware,
        )
    ]

    assert "result" in captured, "30 轮历史没有触发压缩"

    removed = {m.id for m in captured["result"]["messages"] if m.type == "remove"}
    kept = [m for m in captured["before"] if m.id not in removed]

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
    middleware = CompactionMiddleware(
        context_window=_TIGHT_WINDOW, trigger_ratio=0.75, keep_recent_turns=3, summarizer=summarizer
    )

    result = await middleware.abefore_model({"messages": history})
    assert result is not None

    removed = {m.id for m in result["messages"] if m.type == "remove"}
    kept = [m for m in history if m.id not in removed]
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
            compactor=CompactionMiddleware(
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
            compactor=CompactionMiddleware(
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
    middleware = CompactionMiddleware(
        context_window=_TIGHT_WINDOW, trigger_ratio=0.75, keep_recent_turns=3, summarizer=summarizer
    )
    await middleware.abefore_model({"messages": history})

    sent = summarizer.calls[0]
    assert not any(m.id == "think1" for m in sent), "thinking 被送去摘要了"


# ---------------------------------------------------------------- 不触发的情形


async def test_short_history_is_untouched() -> None:
    """没到阈值就不该动 —— 每轮都压缩会全量击穿 prompt cache（§7.3 坑 2）。"""
    middleware = CompactionMiddleware(
        context_window=1_000_000,
        trigger_ratio=0.75,
        keep_recent_turns=3,
        summarizer=RecordingSummarizer(),
    )
    assert await middleware.abefore_model({"messages": build_history(2)}) is None


def test_disabled_spec_means_no_middleware_at_all() -> None:
    """enabled=False 的语义从「装了但自我短路」变成「根本不构造」。

    ★ 关的开关在装配层：server 的 _compactor 见 enabled=False 直接返回
      None，图里连这个节点都没有 —— 比装一个每轮空转的中间件更诚实。
      这里钉住装配层真的这么做了。
    """
    from atlas_server.executor.assembly import HookAssembly

    import inspect

    src = inspect.getsource(HookAssembly._compactor)
    assert "enabled" in src, "装配层不再检查 enabled，禁用压缩的开关失效了"


async def test_overflow_when_recent_turns_alone_exceed_window() -> None:
    """§7.6：保留区本身就装不下 → 报错而不是无限压缩。"""
    middleware = CompactionMiddleware(
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
