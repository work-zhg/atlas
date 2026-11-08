"""横向扩展：run 的派发、租约、检查点、接管与交接（executor/design-run-dispatch.html）。

两个执行器实例 = 同一个进程里两个 RunWorker，各自换一个实例 id。
「实例死了」= 把它的本地任务当成被接管掐掉（不让它碰库），再把租约改成已过期 ——
与进程被 SIGKILL 后租约自然到期在数据库里看到的是同一个状态。
兜底任务、续租是定时任务库的实例任务：这里直接调一轮 execute，不起调度。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import Message, Run, RunEvent
from atlas_agent.db.session import fetch_one, get_sessionmaker
from atlas_agent.executor.compose import build_executor
from atlas_agent.executor.continuation import DanglingCallPolicy, _discarded_chars
from atlas_agent.executor.scheduling import RunWorker
from atlas_agent.main import create_app
from atlas_agent.repositories.run import LeaseLost, RunRepository
from atlas_agent.repositories.run_queue import RunQueue, Taken, TakeoverReason
from atlas_agent.repositories.run_record import RunRecordRepository
from atlas_agent.repositories.thread import ThreadRepository
from atlas_engine.contracts.events import EventFactory, EventType
from atlas_engine.contracts.messages import StoredMessage, dangling_calls, unknown_results
from atlas_engine.runtime.messages import Transcript
from atlas_jobs import JobContext, NoParams
from httpx import ASGITransport
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import PrivateAttr
from sqlalchemy import select, update

from tests.fake_uc import AS_USER

from .fakes import TurnModel, tool_call_chunk

SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": ["write_todos"],
}


# --------------------------------------------------------------------------- 纯计算


def test_checkpoint_flushes_tool_use_before_results_and_batches_results() -> None:
    t = Transcript()
    t.extend([AIMessage(content="", tool_calls=[_call("a"), _call("b")])])
    first = t.checkpoint_rows()
    assert [r.role for r in first] == ["assistant"], "tool_use 要在工具执行前落库（意图记录）"

    t.extend([ToolMessage(content="ra", tool_call_id="a")])
    assert t.checkpoint_rows() == [], "一批结果没收齐不落 —— 一批结果要落成一条"

    t.extend([ToolMessage(content="rb", tool_call_id="b")])
    rows = t.checkpoint_rows()
    assert len(rows) == 1 and rows[0].kind == "tool_result"
    assert {b["tool_use_id"] for b in rows[0].content} == {"a", "b"}
    assert t.checkpoint_rows() == []


def test_checkpoint_does_not_seal_a_running_tool() -> None:
    """工具还在跑时落检查点，不能把它补成「中断」—— 结果马上就来。"""
    t = Transcript()
    t.extend([AIMessage(content="", tool_calls=[_call("a")])])
    rows = t.checkpoint_rows()
    assert all(r.kind != "tool_result" for r in rows)


def test_remaining_rows_seal_calls_flushed_in_an_earlier_checkpoint() -> None:
    """工具执行中这一段就结束了（失败）：悬空的 tool_use 在之前的检查点里，收尾要补结果。"""
    t = Transcript()
    t.extend([AIMessage(content="", tool_calls=[_call("a")])])
    t.checkpoint_rows()
    rows = t.remaining_rows()
    assert len(rows) == 1 and rows[0].kind == "tool_result"
    assert rows[0].content[0]["tool_use_id"] == "a" and rows[0].content[0]["is_error"]


def test_remaining_rows_leave_waiting_calls_dangling() -> None:
    """挂起时在等的调用（审批 / 委派）不补「中断」，即使它的 tool_use 在更早的检查点里 ——
    它故意悬空，续跑时才拿到结果；补了就是两份（contracts/suspension.py）。"""
    t = Transcript()
    t.extend([AIMessage(content="", tool_calls=[_call("a"), _call("b")])])
    t.checkpoint_rows()
    rows = t.remaining_rows(waiting={"b"})
    assert [b["tool_use_id"] for r in rows for b in r.content] == ["a"]


def test_remaining_rows_without_checkpoints_equals_to_storage() -> None:
    t = Transcript()
    t.extend(
        [
            AIMessage(content="", tool_calls=[_call("a")]),
            ToolMessage(content="ra", tool_call_id="a"),
            AIMessage(content="done"),
        ]
    )
    assert t.remaining_rows() == Transcript(messages=list(t.messages)).to_storage()


def test_dangling_calls_for_takeover() -> None:
    rows = [
        StoredMessage(role="user", kind="chat", content=[{"type": "text", "text": "q"}]),
        StoredMessage(
            role="assistant",
            kind="chat",
            content=[
                {"type": "tool_use", "id": "a", "name": "x", "input": {}},
                {"type": "tool_use", "id": "b", "name": "y", "input": {}},
            ],
        ),
        StoredMessage(
            role="user",
            kind="tool_result",
            content=[{"type": "tool_result", "tool_use_id": "a", "content": "ok"}],
        ),
    ]
    assert dangling_calls(rows) == [("b", "y")]
    assert dangling_calls(rows[:1]) == [], "最后是用户的提问：没有悬空"
    sealing = unknown_results(["b"])
    assert [b["tool_use_id"] for b in sealing.content] == ["b"]
    assert "未知" in sealing.content[0]["content"]


def test_only_resumable_tools_are_replayed() -> None:
    """execute 能用同一个调用接上（沙箱的执行账本）；其余工具记「结果未知」。"""
    policy = DanglingCallPolicy()
    assert policy.resolve("execute") == "replay"
    assert [policy.resolve(n) for n in ("write_file", "write_todos", "mcp__x__y", "task")] == [
        "unknown"
    ] * 4


def test_discarded_chars_counts_only_text_after_the_last_checkpointed_event() -> None:
    f = EventFactory(uuid4(), lambda: datetime.now(UTC))
    events = [
        f.make(EventType.MESSAGE_DELTA, {"text": "前面的话"}),
        f.make(EventType.TOOL_COMPLETED, {"call_id": "a"}),
        f.make(EventType.MESSAGE_DELTA, {"text": "半句"}),
        f.make(EventType.MESSAGE_DELTA, {"text": "话"}),
    ]
    assert _discarded_chars(events, 0) == 3
    assert _discarded_chars(events[:2], 0) == 0


def _call(call_id: str) -> dict[str, Any]:
    return {"id": call_id, "name": "write_todos", "args": {}, "type": "tool_call"}


# --------------------------------------------------------------------------- 租约（库）


@pytest.mark.usefixtures("clean_db")
async def test_take_renew_fence_and_takeover() -> None:
    run_id = await _bare_queued_run()
    lease = 30.0
    sm = get_sessionmaker()

    async with sm() as s:
        assert await RunQueue(s).take(run_id, owner="A", lease_s=lease) == Taken(None)
        await s.commit()
    run = await _run(run_id)
    assert run.queued_at is None, "抢到时清掉排队时刻"
    async with sm() as s:
        # 同一个 run 不会被第二个实例抢到
        assert await RunQueue(s).take(run_id, owner="B", lease_s=lease) is None
        await s.commit()

    await _expire(run_id)
    async with sm() as s:
        taken = await RunQueue(s).take(run_id, owner="B", lease_s=lease)
        assert taken == Taken(TakeoverReason.EXPIRED)
        await s.commit()
    async with sm() as s:
        assert await RunQueue(s).renew([run_id], owner="A", lease_s=lease) == set(), (
            "被接管后旧实例续不上"
        )
        with pytest.raises(LeaseLost):
            await RunRecordRepository(s).checkpoint(run_id, owner="A", last_seq=5)
        await s.rollback()
    async with sm() as s:
        queue = RunQueue(s)
        assert await queue.renew([run_id], owner="B", lease_s=lease) == {run_id}
        assert await queue.release(run_id, owner="B")
        await s.commit()
    async with sm() as s:
        assert await RunQueue(s).take(run_id, owner="C", lease_s=lease) == Taken(
            TakeoverReason.HANDOFF
        )
        await s.commit()
    async with sm() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None and run.owner == "C" and run.takeovers == 2


@pytest.mark.usefixtures("clean_db")
async def test_two_instances_taking_the_same_run_only_one_wins() -> None:
    """几个实例同时按 run 号抢同一个 run：只有一个抢得到（SKIP LOCKED，不排队等）。"""
    run_id = await _bare_queued_run()
    sm = get_sessionmaker()

    async def take(owner: str) -> Taken | None:
        async with sm() as s:
            taken = await RunQueue(s).take(run_id, owner=owner, lease_s=30.0)
            await s.commit()
            return taken

    results = await asyncio.gather(*(take(f"i{n}") for n in range(5)))
    assert sum(r is not None for r in results) == 1, results


@pytest.mark.usefixtures("clean_db")
async def test_candidates_skip_fresh_queued_and_page_by_id() -> None:
    """兜底候选：刚排队的不碰（请求那条路正在抢），排队久了的、租约过期的、交出来的才算。"""
    fresh = await _bare_queued_run(queued_at=datetime.now(UTC))
    stale = await _bare_queued_run(queued_at=datetime.now(UTC) - timedelta(seconds=30))
    expired = await _bare_queued_run()
    handed = await _bare_queued_run()
    held = await _bare_queued_run()
    sm = get_sessionmaker()
    async with sm() as s:
        queue = RunQueue(s)
        for run_id in (expired, handed, held):
            assert await queue.take(run_id, owner="A", lease_s=30.0)
        assert await queue.release(handed, owner="A")
        await s.commit()
    await _expire(expired)

    async with sm() as s:
        queue = RunQueue(s)
        first = await queue.candidates(after_id=0, limit=2, queued_for_s=5.0)
        rest = await queue.candidates(after_id=first[-1].id, limit=2, queued_for_s=5.0)
        tail = await queue.candidates(after_id=rest[-1].id, limit=2, queued_for_s=5.0)
    assert [c.run_uuid for c in first + rest] == [stale, expired, handed]
    assert tail == []
    assert fresh not in {c.run_uuid for c in first + rest}
    assert held not in {c.run_uuid for c in first + rest}


# --------------------------------------------------------------------------- 接管（端到端）


class HangingModel(TurnModel):
    """第 hang_from 次调用起挂住 —— 模拟实例在模型调用中途死掉 / 停机。"""

    hang_from: int = 2
    _gate: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        script = self._script()
        if self.call_count >= self.hang_from:
            await self._gate.wait()
        for chunk in script:
            yield ChatGenerationChunk(message=chunk)


#: RecordingModel 每次调用看到的消息（模块级：pydantic 字段的默认值会被逐实例拷贝）
SEEN: list[list[BaseMessage]] = []


class RecordingModel(TurnModel):
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        SEEN.append(list(messages))
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


@pytest.fixture
async def executors() -> AsyncIterator[list[RunWorker]]:
    made: list[RunWorker] = []
    yield made
    for ex in made:
        await ex.shutdown()


def _executor(
    made: list[RunWorker], name: str, builder: Any, **overrides: Any
) -> RunWorker:
    settings = get_settings().model_copy(update=overrides) if overrides else get_settings()
    # 同一个进程里扮演两个实例
    ex = build_executor(get_sessionmaker(), settings, model_builder=builder, instance=name)
    made.append(ex)
    return ex


@pytest.mark.usefixtures("clean_db")
async def test_expired_lease_is_taken_over_from_the_last_checkpoint(
    executors: list[RunWorker],
) -> None:
    """A 跑完一个工具轮次（已落检查点）后在下一次模型调用中死掉 → B 接着跑，不重做工具。"""
    a = _executor(
        executors,
        "instance-a",
        lambda *_a, **_k: HangingModel(
            scripts=[[tool_call_chunk("write_todos", '{"todos":[]}', "c1")], []]
        ),
    )
    async with _client(a) as client:
        run_id = await _start(client)
        await _until(lambda: _messages_of(run_id), lambda rows: len(rows) >= 2)
        _kill(a, run_id)
        await _expire(run_id)

        SEEN.clear()
        b = _executor(
            executors,
            "instance-b",
            lambda *_a, **_k: RecordingModel(scripts=[[AIMessageChunk(content="接着办完了")]]),
        )
        await _recover(b)
        run = await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")

    assert run.takeovers == 1 and run.owner == "instance-b"
    rows = await _messages_of(run_id)
    kinds = [(r.role, r.kind) for r in rows]
    assert kinds == [
        ("assistant", "chat"),
        ("user", "tool_result"),
        ("assistant", "chat"),
    ], f"工具轮次被重做或丢失：{kinds}"
    # B 的第一次模型调用看到的是 A 落库的那一轮（工具结果在历史里，不再调一次）
    first = SEEN[0]
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "c1" for m in first)
    resumed = await _archived(run_id, EventType.RUN_RESUMED)
    assert resumed and resumed[0].data["reason"] == "expired"
    seqs = [e.seq for e in await _archived(run_id)]
    assert len(seqs) == len(set(seqs)), "接管后 run 内序号重号"


@pytest.mark.usefixtures("clean_db")
async def test_takeover_marks_a_tool_that_was_running_as_unknown(
    executors: list[RunWorker],
) -> None:
    """死在工具执行中：tool_use 已落库、结果没有 → 接管者补「结果未知」，不让模型不知情地重放。"""
    a = _executor(
        executors, "instance-a", lambda *_a, **_k: HangingModel(hang_from=1, scripts=[[]])
    )
    async with _client(a) as client:
        run_id = await _start(client)
        await asyncio.sleep(0.3)
        _kill(a, run_id)
        # 等价于 A 在工具执行前落下的意图记录
        async with get_sessionmaker()() as s:
            run = await RunRepository(s).get(run_id)
            assert run is not None
            await ThreadRepository(s).add_messages(
                thread_id=run.thread_uuid,
                messages=[
                    StoredMessage(
                        role="assistant",
                        kind="chat",
                        content=[
                            {"type": "tool_use", "id": "c9", "name": "write_todos", "input": {}}
                        ],
                    )
                ],
                run_id=run_id,
            )
            await s.commit()
        await _expire(run_id)

        SEEN.clear()
        b = _executor(
            executors,
            "instance-b",
            lambda *_a, **_k: RecordingModel(scripts=[[AIMessageChunk(content="我先检查一下")]]),
        )
        await _recover(b)
        await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")

    tool_results = [
        m for m in SEEN[0] if isinstance(m, ToolMessage) and m.tool_call_id == "c9"
    ]
    assert len(tool_results) == 1
    assert "未知" in str(tool_results[0].content) and tool_results[0].status == "error"


@pytest.mark.usefixtures("clean_db")
async def test_takeover_replays_a_resumable_tool_with_the_same_call(
    executors: list[RunWorker], monkeypatch: pytest.MonkeyPatch
) -> None:
    """死在工具执行中、工具声明了能接上：接管者用同一个 tool_call_id 重放，而不是补「结果未知」。

    生产上能接上的是 execute（沙箱的执行账本按同一个调用接上原命令，见 test_sandbox_provider）；
    这里把 write_todos 声明成能接上，只验证接管的这一侧：同一个调用被执行、结果落库、模型看到它。
    """
    import dataclasses

    from atlas_engine.runtime import tool_registry

    monkeypatch.setattr(
        tool_registry,
        "BUILTIN_TOOLS",
        tuple(
            dataclasses.replace(t, resumable=True) if t.name == "write_todos" else t
            for t in tool_registry.BUILTIN_TOOLS
        ),
    )
    a = _executor(
        executors, "instance-a", lambda *_a, **_k: HangingModel(hang_from=1, scripts=[[]])
    )
    async with _client(a) as client:
        run_id = await _start(client)
        await asyncio.sleep(0.3)
        _kill(a, run_id)
        async with get_sessionmaker()() as s:
            run = await RunRepository(s).get(run_id)
            assert run is not None
            await ThreadRepository(s).add_messages(
                thread_id=run.thread_uuid,
                messages=[
                    StoredMessage(
                        role="assistant",
                        kind="chat",
                        content=[
                            {
                                "type": "tool_use",
                                "id": "c9",
                                "name": "write_todos",
                                "input": {"todos": [{"content": "接着做", "status": "pending"}]},
                            }
                        ],
                    )
                ],
                run_id=run_id,
            )
            await s.commit()
        await _expire(run_id)

        SEEN.clear()
        b = _executor(
            executors,
            "instance-b",
            lambda *_a, **_k: RecordingModel(scripts=[[AIMessageChunk(content="继续")]]),
        )
        await _recover(b)
        await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")

    results = [m for m in SEEN[0] if isinstance(m, ToolMessage) and m.tool_call_id == "c9"]
    assert len(results) == 1, results
    assert "未知" not in str(results[0].content) and "接着做" in str(results[0].content)
    rows = await _messages_of(run_id)
    stored = [
        b
        for r in rows
        if r.kind == "tool_result"
        for b in r.content
        if b.get("tool_use_id") == "c9"
    ]
    assert len(stored) == 1 and not stored[0].get("is_error"), stored


@pytest.mark.usefixtures("clean_db")
async def test_drain_hands_the_run_to_another_instance(
    executors: list[RunWorker],
) -> None:
    """停机交接：等不到安全点就中止并清租约，别的实例立刻以 handoff 领走、跑完。"""
    a = _executor(
        executors,
        "instance-a",
        lambda *_a, **_k: HangingModel(
            scripts=[[tool_call_chunk("write_todos", '{"todos":[]}', "c1")], []]
        ),
        drain_timeout_s=0.5,
    )
    async with _client(a) as client:
        run_id = await _start(client)
        await _until(lambda: _messages_of(run_id), lambda rows: len(rows) >= 2)
        await a.drain()

        handed = await _run(run_id)
        assert handed.status == "running" and handed.owner is None, "交接后状态不变、租约清空"

        b = _executor(
            executors,
            "instance-b",
            lambda *_a, **_k: TurnModel(scripts=[[AIMessageChunk(content="接着办完了")]]),
        )
        await _recover(b)
        run = await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")
    assert run.owner == "instance-b"
    resumed = await _archived(run_id, EventType.RUN_RESUMED)
    assert resumed and resumed[0].data["reason"] == "handoff"


@pytest.mark.usefixtures("clean_db")
async def test_lost_lease_stops_local_execution_without_touching_the_run(
    executors: list[RunWorker],
) -> None:
    """续租发现租约已在别人手里 → 本地立即停，不落库、不放锁（假死复活不能双写）。"""
    a = _executor(
        executors, "instance-a", lambda *_a, **_k: HangingModel(hang_from=1, scripts=[[]])
    )
    async with _client(a) as client:
        run_id = await _start(client)
        await _until(lambda: _run(run_id), lambda r: r.status == "running")
        slot = a._slots[run_id]
        async with get_sessionmaker()() as s:
            await s.execute(update(Run).where(Run.run_uuid == run_id).values(owner="someone-else"))
            await s.commit()
        await a.renewer.execute(_ctx(), NoParams())
        assert slot.task is not None
        await asyncio.wait([slot.task], timeout=5)
        assert slot.task.done() and slot.lost

    run = await _run(run_id)
    assert run.status == "running" and run.owner == "someone-else", "旧实例碰了已被接管的 run"


# --------------------------------------------------------------------------- 派发与兜底


@pytest.mark.usefixtures("clean_db")
async def test_a_full_instance_leaves_the_run_for_another_instances_recovery(
    executors: list[RunWorker],
) -> None:
    """接请求的实例满了：派发不报错，run 留在库里排队；排队超过几秒，别的实例的兜底任务接走。"""
    a = _executor(
        executors,
        "instance-a",
        lambda *_a, **_k: TurnModel(scripts=[[AIMessageChunk(content="不该是我")]]),
        max_concurrent_runs=0,
    )
    async with _client(a) as client:
        run_id = await _start(client)
        queued = await _run(run_id)
        assert queued.status == "queued" and queued.owner is None and queued.queued_at

        patient = _executor(executors, "instance-b", lambda *_a, **_k: TurnModel(scripts=[]))
        await _recover(patient)
        assert (await _run(run_id)).status == "queued", "刚排队的不该被兜底任务抢"

        c = _executor(
            executors,
            "instance-c",
            lambda *_a, **_k: TurnModel(scripts=[[AIMessageChunk(content="我来办")]]),
            run_recovery_queued_s=0.0,
        )
        await _recover(c)
        run = await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")
    assert run.owner == "instance-c" and run.takeovers == 0


@pytest.mark.usefixtures("clean_db")
async def test_recovery_stops_taking_when_the_instance_is_full(
    executors: list[RunWorker],
) -> None:
    """兜底任务抢一个占一个槽，没空槽就停：剩下的留给别的实例。"""
    runs = [await _bare_queued_run() for _ in range(3)]
    b = _executor(
        executors, "instance-b", lambda *_a, **_k: TurnModel(scripts=[]), max_concurrent_runs=1
    )
    await _recover(b)
    states = [await _run(run_id) for run_id in runs]
    assert sum(r.owner == "instance-b" for r in states) == 1, [r.status for r in states]
    assert sum(r.status == "queued" for r in states) == 2


@pytest.mark.usefixtures("clean_db")
async def test_a_draining_instance_takes_nothing(executors: list[RunWorker]) -> None:
    """停机中关了准入：派发留在库里，兜底任务也不抢。"""
    a = _executor(
        executors, "instance-a", lambda *_a, **_k: TurnModel(scripts=[]), run_recovery_queued_s=0.0
    )
    await a.drain()
    run_id = await _bare_queued_run()
    await a.dispatch(run_id)
    await _recover(a)
    assert (await _run(run_id)).status == "queued"


async def test_api_role_runs_no_recovery() -> None:
    """api 角色不执行 run：并发上限为 0、不注册兜底任务；续租照常注册（没有 run 就什么都不做）。"""
    from atlas_agent.executor.compose import register_jobs
    from atlas_jobs import JobScheduler

    for role, expected in (
        ("all", {"agent.run_recovery", "agent.run_lease_renewer"}),
        ("api", {"agent.run_lease_renewer"}),
    ):
        settings = get_settings().model_copy(update={"server_role": role})
        worker = build_executor(get_sessionmaker(), settings, instance=f"{role}-1")
        jobs = JobScheduler()
        register_jobs(jobs, worker, settings)
        jobs.start()
        try:
            assert {m.type for m in jobs.metrics()} == expected
        finally:
            await jobs.stop()
        assert worker.has_free_slot() is (role != "api")


# --------------------------------------------------------------------------- 工具


def _client(executor: RunWorker) -> httpx.AsyncClient:
    app = create_app()
    app.state.executor = executor
    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AS_USER
    )


async def _start(client: httpx.AsyncClient) -> UUID:
    agent = await client.post(
        "/v1/agents", json={"slug": f"hs-{uuid4().hex[:6]}", "name": "横向", "spec": SPEC}
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"], "title": "t"})
    res = await client.post(
        f"/v1/threads/{thread.json()['id']}/runs",
        json={"content": [{"type": "text", "text": "记个待办"}]},
    )
    assert res.status_code == 202, res.text
    return UUID(res.json()["run_id"])


def _kill(executor: RunWorker, run_id: UUID) -> None:
    """实例死了：本地任务停掉，且不让它再碰库（被接管的语义）。"""
    slot = executor._slots[run_id]
    slot.lost = True
    assert slot.task is not None
    slot.task.cancel()


async def _expire(run_id: UUID) -> None:
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Run)
            .where(Run.run_uuid == run_id)
            .values(lease_until=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()


async def _run(run_id: UUID) -> Run:
    async with get_sessionmaker()() as s:
        run = await fetch_one(s, Run, Run.run_uuid == run_id)
        assert run is not None
        return run


async def _messages_of(run_id: UUID) -> list[Message]:
    async with get_sessionmaker()() as s:
        stmt = select(Message).where(Message.run_uuid == run_id).order_by(Message.created_at)
        return list((await s.execute(stmt)).scalars())


async def _archived(run_id: UUID, kind: EventType | None = None) -> list[RunEvent]:
    async with get_sessionmaker()() as s:
        stmt = select(RunEvent).where(RunEvent.run_uuid == run_id).order_by(RunEvent.thread_seq)
        if kind is not None:
            stmt = stmt.where(RunEvent.type == kind.value)
        return list((await s.execute(stmt)).scalars())


async def _until(fetch: Any, ok: Any, *, timeout: float = 20.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await fetch()
        if ok(last):
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"等不到期望状态，最后一次：{last!r}")


async def _bare_queued_run(*, queued_at: datetime | None = None) -> UUID:
    """一个排队中的 run（不经 API —— 只测仓库层的抢锁语义）。queued_at 为空 = 迁移前的旧数据。"""
    async with get_sessionmaker()() as s:
        run = Run(
            run_uuid=uuid4(),
            thread_uuid=uuid4(),
            agent_version_uuid=uuid4(),
            status="queued",
            queued_at=queued_at,
        )
        s.add(run)
        await s.commit()
        return run.run_uuid


def _ctx() -> JobContext:
    return JobContext(job_name="test", fired_at=datetime.now(UTC))


async def _recover(executor: RunWorker) -> None:
    """这个实例的兜底任务跑一轮。"""
    await executor.recovery.execute(_ctx(), NoParams())
