"""续跑不产生重复的 tool_result，也不丢结果（2026-10-13 智能体评测冒烟撞到的路径）。

① 并行一批里一个挂起：续跑时已完成的兄弟调用被原样返回（kernel 不重跑），
   runner 不能再发事件、不能再落库 —— test_suspension_middleware 覆盖 kernel 那半。
② 一轮两次审批：第一次的等待项在 run.waiting_on 里可能还留着（它留到 run 结束才清），
   而那个调用早已有了结果；第二次续跑时它不能再交给图。
③ 续跑跳 tools 时 tools 节点的更新是**列表**（有中间件返回 Command 时），runner 要认。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest
from atlas_agent.executor import inprocess
from atlas_agent.executor.inprocess import InProcessExecutor
from atlas_agent.executor.runner import Answer, EventFactory, _from_updates, _transcript_messages
from langchain_core.messages import AIMessage, ToolMessage

AP_1 = str(uuid4())
AP_2 = str(uuid4())


def _ai(*ids: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": i, "name": "execute", "args": {}} for i in ids])


def _events() -> EventFactory:
    return EventFactory(uuid4(), lambda: datetime.now(UTC), start_seq=0)


def test_runner_skips_results_of_settled_calls() -> None:
    chunk = {
        "tools": {
            "messages": [
                ToolMessage(content="old", tool_call_id="a", name="write_file"),
                ToolMessage(content="new", tool_call_id="b", name="execute"),
            ]
        }
    }
    out = _from_updates(chunk, _events(), 0, Answer(), frozenset({"a"}))
    assert [e.data["call_id"] for e in out] == ["b"]


def test_runner_reads_list_shaped_tool_updates() -> None:
    """★ 续跑跳 tools 时，只要有中间件返回 Command（审批放行、委派填结论），整批更新就以
    列表交出（每个调用一份）。只认 dict 的话，这一段执行出来的结果既不发事件、也不落库。"""
    chunk = {
        "tools": [
            {"messages": [ToolMessage(content="old", tool_call_id="a", name="write_file")]},
            {"messages": [ToolMessage(content="ran", tool_call_id="b", name="execute")]},
            {"waiting_on": {"b": None}},
        ]
    }
    assert [m.tool_call_id for m in _transcript_messages(chunk)] == ["a", "b"]
    out = _from_updates(chunk, _events(), 0, Answer(), frozenset({"a"}))
    assert [e.data["call_id"] for e in out] == ["b"]


class _FakeApprovals:
    """approval 表：token → 状态。不在表里 = 行丢了。"""

    rows: ClassVar[dict[str, str]] = {}

    def __init__(self, _session: Any) -> None:
        pass

    async def get(self, approval_id: UUID) -> Any:
        status = self.rows.get(str(approval_id))
        return None if status is None else SimpleNamespace(status=status, outcome=None)


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> type[_FakeApprovals]:
    monkeypatch.setattr(inprocess, "ApprovalRepository", _FakeApprovals)
    _FakeApprovals.rows = {}
    return _FakeApprovals


async def _resume_state(waiting_on: Any, history: list[Any]) -> tuple[dict, dict]:
    run = SimpleNamespace(id=uuid4(), waiting_on=waiting_on)
    # 只走审批分支，不碰 session / self 的其它部分
    executor = InProcessExecutor.__new__(InProcessExecutor)
    return await executor._resume_state(None, run, history)  # type: ignore[arg-type]


async def test_second_resume_drops_waits_that_already_have_a_result(approvals: Any) -> None:
    """★ 一轮两次审批：c1 在第一次续跑时已经执行，但 run.waiting_on 里的旧项还在。

    交给图的话 before_model 会为它跳 tools，而 ToolNode 执行的是**最后一条** AIMessage 的
    调用 —— 结果是 c2 被提前执行或重复执行。
    """
    approvals.rows = {AP_1: "approved", AP_2: "approved"}
    history = [
        _ai("c1"),
        ToolMessage(content="first result", tool_call_id="c1"),
        _ai("c2"),
    ]
    waits = {
        "c1": {"reason": "approval", "token": AP_1},
        "c2": {"reason": "approval", "token": AP_2},
    }
    waiting_on, decisions = await _resume_state(waits, history)
    assert waiting_on == {"c2": {"reason": "approval", "token": AP_2}}
    assert decisions == {AP_2: {"status": "approved"}}


async def test_only_settled_approvals_go_into_decisions(approvals: Any) -> None:
    """★ pending 不进 decisions：放进去的话 before_model 放行、审批中间件又登记一次，
    两边来回跳到 GraphRecursionError（demo/suspend-resume 实验 3′）。

    行丢了按 failed（门禁故障）：关卡已判它结清，图里却拿不到决策的话，图跳出、关卡放行，死循环。
    """
    approvals.rows = {AP_1: "pending"}
    history = [_ai("c1", "c2")]
    waits = {
        "c1": {"reason": "approval", "token": AP_1},
        "c2": {"reason": "approval", "token": AP_2},  # 表里没有这一行
    }
    waiting_on, decisions = await _resume_state(waits, history)
    assert set(waiting_on) == {"c1", "c2"}
    assert decisions == {AP_2: {"status": "failed"}}


async def test_old_array_shaped_waiting_on_is_ignored(approvals: Any) -> None:
    """旧格式（数组）的遗留数据当成没有等待项 —— 不续跑它，也不让它炸掉 _prepare。"""
    assert await _resume_state([{"reason": "approval", "token": AP_1}], [_ai("c1")]) == ({}, {})


async def test_resubmit_keeps_strong_ref_to_new_task() -> None:
    """续跑时同一个 run_id 被再次领取：上一段任务结束不能把新任务的引用删掉。"""
    import asyncio

    ex = InProcessExecutor.__new__(InProcessExecutor)
    ex._slots = {}
    # 续租任务已在跑（这里用一个永不完成的 future 占位，不让 _spawn 去起真的）
    ex._renewer = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def guarded(run_id: object, slot: object, takeover: object = None) -> None:
        await release.wait()

    ex._guarded = guarded  # type: ignore[method-assign]

    rid = uuid4()
    ex._spawn(rid, None)
    first = ex._slots[rid]
    first.task.cancel()  # 上一段先结束
    ex._spawn(rid, None)  # 续跑段
    second = ex._slots[rid]
    await asyncio.sleep(0)
    assert ex._slots.get(rid) is second, "上一段的回调删掉了续跑段的引用"
    release.set()
    await second.task
    assert rid not in ex._slots
