"""续跑不产生重复的 tool_result（2026-10-13 智能体评测冒烟撞到的两条路径）。

① 并行一批里一个挂起：续跑时已完成的兄弟调用被原样返回（kernel 不重跑），
   runner 不能再发事件、不能再落库 —— test_suspension_middleware 覆盖 kernel 那半。
② 一轮两次审批：第一次的哨兵在表里留着、后面跟着真实结果；第二次续跑重建历史时
   它不能再被换成占位。
"""

from __future__ import annotations

from atlas_engine.contracts import is_released, released_marker, suspend_marker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.executor.runner import EventFactory, _from_updates, _settled_call_ids
from langchain_core.messages import AIMessage, ToolMessage


def _ai(*ids: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": i, "name": "execute", "args": {}} for i in ids])


def test_settled_ids_exclude_markers() -> None:
    history = [
        _ai("a", "b", "c"),
        ToolMessage(content="done", tool_call_id="a"),
        ToolMessage(content=suspend_marker("approval", "ap-b"), tool_call_id="b"),
        ToolMessage(content=released_marker("ap-c"), tool_call_id="c"),
    ]
    assert _settled_call_ids(history) == {"a"}


def test_runner_skips_results_of_settled_calls() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from atlas_server.executor.runner import Answer

    events = EventFactory(uuid4(), lambda: datetime.now(UTC), start_seq=0)
    chunk = {
        "tools": {
            "messages": [
                ToolMessage(content="old", tool_call_id="a", name="write_file"),
                ToolMessage(content="new", tool_call_id="b", name="execute"),
            ]
        }
    }
    out = _from_updates(chunk, events, {}, 0, Answer(), frozenset({"a"}))
    assert [e.data["call_id"] for e in out] == ["b"]


async def test_second_resume_drops_settled_sentinel() -> None:
    history = [
        _ai("c1"),
        ToolMessage(content=suspend_marker("approval", "ap-1"), tool_call_id="c1"),
        ToolMessage(content="first result", tool_call_id="c1"),  # 第一次续跑补上的
        _ai("c2"),
        ToolMessage(content=suspend_marker("approval", "ap-2"), tool_call_id="c2"),
    ]
    # 只走审批分支，不碰 session / self
    await InProcessExecutor._settle_pending(None, None, history)  # type: ignore[arg-type]
    tools = [(m.tool_call_id, m.content) for m in history if isinstance(m, ToolMessage)]
    assert tools[0] == ("c1", "first result")
    assert len([t for t in tools if t[0] == "c1"]) == 1
    assert tools[1][0] == "c2" and is_released(tools[1][1])


async def test_resubmit_keeps_strong_ref_to_new_task() -> None:
    """续跑时同一个 run_id 被再次 submit：上一段任务结束不能把新任务的引用删掉。"""
    import asyncio

    ex = InProcessExecutor.__new__(InProcessExecutor)
    ex._tasks = {}
    release = asyncio.Event()

    async def guarded(run_id: object) -> None:
        await release.wait()

    ex._guarded = guarded  # type: ignore[method-assign]
    from uuid import uuid4

    rid = uuid4()
    await ex.submit(rid)
    first = ex._tasks[rid]
    first.cancel()  # 上一段先结束
    await ex.submit(rid)  # 续跑段
    second = ex._tasks[rid]
    await asyncio.sleep(0)
    assert ex._tasks.get(rid) is second, "上一段的回调删掉了续跑段的引用"
    release.set()
    await second
    assert rid not in ex._tasks
