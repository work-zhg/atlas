"""LangGraph updates 流 → TraceEvent 的映射（P4）。

★ 这里的输入形状**取自对真实 kernel 图的实测**（见提交信息里的探针），
不是照文档猜的。单测锁住映射，真实工具链路另由 test_live_gateway 兜底。
"""

from __future__ import annotations

from atlas_agent.domain.translator import tool_calls_from, tool_result_from
from langchain_core.messages import AIMessage, ToolMessage


def test_tool_calls_from_ai_message() -> None:
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "write_todos", "args": {"todos": ["a", "b"]}, "id": "toolu_01"}],
    )
    calls = tool_calls_from(msg)
    assert len(calls) == 1
    assert calls[0]["call_id"] == "toolu_01"
    assert calls[0]["name"] == "write_todos"
    assert "todos=" in calls[0]["args_preview"]


def test_tool_calls_from_plain_message_is_empty() -> None:
    assert tool_calls_from(AIMessage(content="没有工具调用")) == []


def test_args_preview_is_truncated() -> None:
    """收起态不能塞进整个 SQL / 文件内容。"""
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "write_file", "args": {"content": "x" * 5000}, "id": "t1"}],
    )
    preview = tool_calls_from(msg)[0]["args_preview"]
    assert len(preview) <= 120
    assert tool_calls_from(msg)[0]["args"]["content"] == "x" * 5000  # 原始值仍保留


def test_tool_result_success() -> None:
    msg = ToolMessage(content="ok", tool_call_id="toolu_01", name="write_todos")
    res = tool_result_from(msg)
    assert res is not None
    assert res["call_id"] == "toolu_01"
    assert res["status"] == "success"


def test_tool_result_error_is_distinguishable() -> None:
    msg = ToolMessage(content="boom", tool_call_id="t2", name="read_file", status="error")
    res = tool_result_from(msg)
    assert res is not None and res["status"] == "error"


def test_tool_result_from_non_tool_message_is_none() -> None:
    assert tool_result_from(AIMessage(content="hi")) is None


# ---------------------------------------------------------------- 哪些更新算「新动作」


def _spec_with(tool_names: tuple[str, ...], **over: object) -> object:
    from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec

    return AgentSpec(
        slug="mapping",
        name="映射",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=tool_names,
        limits=LimitSpec(timeout_s=30),
        **over,  # type: ignore[arg-type]
    )


async def test_patched_history_is_not_replayed() -> None:
    """上一个 run 在等 SubAgent 时被取消，历史末尾留下没结果的 task 调用。

    新 run 开头 PatchToolCallsMiddleware 补「已取消」并整段写回历史。那里面全是
    上一个 run 的调用 —— 本 run 再发 started / finished 的话，前端多出委派框和
    永远转圈的工具卡片（112 上会话 e3ced303 的第 2 个 run 就多了两个框）。
    """
    from uuid import uuid4

    from atlas_agent.domain.events import EventType
    from atlas_agent.domain.spec import ModelSpec, SubAgentSpec
    from langchain_core.messages import AIMessageChunk, HumanMessage

    from .fakes import Spawner, TurnModel
    from .graphs import run_agent

    def call(name: str, call_id: str, **args: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])

    history = [
        HumanMessage(content="做原型"),
        call("task", "old_task", description="第一次", subagent_type="researcher"),
        ToolMessage(content="做完了", tool_call_id="old_task", name="task"),
        call("read_file", "old_read", file_path="/workspace/a"),
        ToolMessage(content="内容", tool_call_id="old_read", name="read_file"),
        call("task", "dangling", description="第二次", subagent_type="researcher"),
    ]
    researcher = SubAgentSpec(
        name="researcher",
        description="做调研",
        system_prompt="你是研究员。",
        model=ModelSpec(model="claude-sonnet-5"),
    )
    events = [
        e
        async for e in run_agent(
            _spec_with(("task",), subagents=(researcher,)),
            TurnModel(scripts=[[AIMessageChunk(content="好的。")]]),
            run_id=uuid4(),
            input_content="你只给了一句话？",
            history=history,
            spawner=Spawner(),
        )
    ]

    types = [e.type for e in events]
    assert types[-1] is EventType.RUN_FINISHED, types
    replayed = {
        EventType.SUBAGENT_STARTED,
        EventType.SUBAGENT_FINISHED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_FAILED,
    }
    assert [e.data for e in events if e.type in replayed] == []


async def test_results_added_by_a_hook_still_reach_the_frontend() -> None:
    """不是所有 hook 节点的更新都是改写历史：TodoListMiddleware.after_model 拒绝
    并行的 write_todos 时**新增**两条错误结果。它们必须照常发 tool.failed ——
    这正是「整段改写才跳过」而不是「只认 model / tools 节点」的原因。
    """
    from uuid import uuid4

    from atlas_agent.domain.events import EventType
    from langchain_core.messages import AIMessageChunk

    from .fakes import TurnModel
    from .graphs import run_agent

    args = '{"todos": [{"content": "a", "status": "pending"}]}'
    parallel = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": "write_todos", "args": args, "id": "t1", "index": 0},
            {"name": "write_todos", "args": args, "id": "t2", "index": 1},
        ],
    )
    events = [
        e
        async for e in run_agent(
            _spec_with(("write_todos",)),
            TurnModel(scripts=[[parallel], [AIMessageChunk(content="好的。")]]),
            run_id=uuid4(),
            input_content="列个计划",
        )
    ]

    started = [e.data["call_id"] for e in events if e.type is EventType.TOOL_STARTED]
    failed = [e.data["call_id"] for e in events if e.type is EventType.TOOL_FAILED]
    assert sorted(started) == ["t1", "t2"]
    assert sorted(failed) == ["t1", "t2"]


def test_rewrite_in_the_middle_of_a_run_does_not_repeat_started() -> None:
    """压缩在 run 中途整段写回历史：本段早先报过的调用会再出现一次，不能再报 started。

    整图里不好让压缩恰好在本段的工具调用之后触发，这里直接喂两帧 updates：
    第一帧是模型发起 c1，第二帧是压缩写回的「摘要 + 保留区」（含 c1 与它的结果）。
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from atlas_agent.domain.events import EventType
    from atlas_agent.executor.runner import Answer, EventFactory, _from_updates
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    events = EventFactory(uuid4(), lambda: datetime.now(UTC), start_seq=0)
    announced: set[str] = set()
    call = AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "c1"}])
    result = ToolMessage(content="内容", tool_call_id="c1", name="read_file")

    first = _from_updates({"model": {"messages": [call]}}, events, 0, Answer(), announced=announced)
    rewritten = [RemoveMessage(id=REMOVE_ALL_MESSAGES), AIMessage(content="摘要"), call, result]
    second = _from_updates(
        {"CompactionMiddleware.before_model": {"messages": rewritten}},
        events,
        0,
        Answer(),
        frozenset({"c1"}),  # c1 的结果本段已报过（run() 里 settled 会记下）
        announced=announced,
    )

    assert [e.type for e in first] == [EventType.TOOL_STARTED]
    assert second == []
