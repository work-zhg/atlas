"""横向扩展：run 的领取、租约、检查点、接管与交接（doc/horizontal-scaling-design.html）。

两个执行器实例 = 同一个进程里两个 InProcessExecutor，各自换一个实例 id。
「实例死了」= 把它的本地任务当成被接管掐掉（不让它碰库），再把租约改成已过期 ——
与进程被 SIGKILL 后租约自然到期在数据库里看到的是同一个状态。
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
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.domain.events import EventFactory, EventType
from atlas_agent.domain.messages import StoredMessage, Transcript, interrupted_results
from atlas_agent.executor.inprocess import InProcessExecutor, _discarded_chars
from atlas_agent.main import create_app
from atlas_agent.repositories.run import LeaseLost, RunRepository
from atlas_agent.repositories.thread import ThreadRepository
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


def test_interrupted_results_for_takeover() -> None:
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
    sealing = interrupted_results(rows)
    assert sealing is not None
    assert [b["tool_use_id"] for b in sealing.content] == ["b"]
    assert "未知" in sealing.content[0]["content"]
    assert interrupted_results(rows[:1]) is None, "最后是用户的提问：没有悬空"


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
async def test_claim_renew_fence_and_takeover() -> None:
    run_id = await _bare_queued_run()
    lease = 30.0
    sm = get_sessionmaker()

    async with sm() as s:
        runs = RunRepository(s)
        assert await _claim_until(runs, run_id, "A", lease) is None
        await s.commit()
    async with sm() as s:
        # 同一个 run 不会被第二个实例领到
        assert await _claim_until(RunRepository(s), run_id, "B", lease) is False
        await s.commit()

    await _expire(run_id)
    async with sm() as s:
        assert await _claim_until(RunRepository(s), run_id, "B", lease) == "expired"
        await s.commit()
    async with sm() as s:
        runs = RunRepository(s)
        assert await runs.renew_leases([run_id], owner="A", lease_s=lease) == set(), (
            "被接管后旧实例续不上"
        )
        with pytest.raises(LeaseLost):
            await runs.checkpoint(run_id, owner="A", last_seq=5)
        await s.rollback()
    async with sm() as s:
        runs = RunRepository(s)
        assert await runs.renew_leases([run_id], owner="B", lease_s=lease) == {run_id}
        assert await runs.release_lease(run_id, owner="B")
        await s.commit()
    async with sm() as s:
        assert await _claim_until(RunRepository(s), run_id, "C", lease) == "handoff"
        await s.commit()
    async with sm() as s:
        run = await RunRepository(s).get(run_id)
        assert run is not None and run.owner == "C" and run.takeovers == 2


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
async def executors() -> AsyncIterator[list[InProcessExecutor]]:
    made: list[InProcessExecutor] = []
    yield made
    for ex in made:
        await ex.shutdown()


def _executor(
    made: list[InProcessExecutor], name: str, builder: Any, **overrides: Any
) -> InProcessExecutor:
    settings = get_settings().model_copy(update=overrides) if overrides else get_settings()
    ex = InProcessExecutor(get_sessionmaker(), settings, model_builder=builder)
    ex._instance = name  # 同一个进程里扮演两个实例
    made.append(ex)
    return ex


@pytest.mark.usefixtures("clean_db")
async def test_expired_lease_is_taken_over_from_the_last_checkpoint(
    executors: list[InProcessExecutor],
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
        assert await b.claim_available() >= 1
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
    executors: list[InProcessExecutor],
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
                thread_id=run.thread_id,
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
        await b.claim_available()
        await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")

    tool_results = [
        m for m in SEEN[0] if isinstance(m, ToolMessage) and m.tool_call_id == "c9"
    ]
    assert len(tool_results) == 1
    assert "未知" in str(tool_results[0].content) and tool_results[0].status == "error"


@pytest.mark.usefixtures("clean_db")
async def test_drain_hands_the_run_to_another_instance(
    executors: list[InProcessExecutor],
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
        await b.claim_available()
        run = await _until(lambda: _run(run_id), lambda r: r.status == "succeeded")
    assert run.owner == "instance-b"
    resumed = await _archived(run_id, EventType.RUN_RESUMED)
    assert resumed and resumed[0].data["reason"] == "handoff"


@pytest.mark.usefixtures("clean_db")
async def test_lost_lease_stops_local_execution_without_touching_the_run(
    executors: list[InProcessExecutor],
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
            await s.execute(update(Run).where(Run.id == run_id).values(owner="someone-else"))
            await s.commit()
        await a.renew_leases()
        assert slot.task is not None
        await asyncio.wait([slot.task], timeout=5)
        assert slot.task.done() and slot.lost

    run = await _run(run_id)
    assert run.status == "running" and run.owner == "someone-else", "旧实例碰了已被接管的 run"


# --------------------------------------------------------------------------- 工具


def _client(executor: InProcessExecutor) -> httpx.AsyncClient:
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


def _kill(executor: InProcessExecutor, run_id: UUID) -> None:
    """实例死了：本地任务停掉，且不让它再碰库（被接管的语义）。"""
    slot = executor._slots[run_id]
    slot.lost = True
    assert slot.task is not None
    slot.task.cancel()


async def _expire(run_id: UUID) -> None:
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(lease_until=datetime.now(UTC) - timedelta(minutes=1))
        )
        await s.commit()


async def _run(run_id: UUID) -> Run:
    async with get_sessionmaker()() as s:
        run = await s.get(Run, run_id)
        assert run is not None
        return run


async def _messages_of(run_id: UUID) -> list[Message]:
    async with get_sessionmaker()() as s:
        stmt = select(Message).where(Message.run_id == run_id).order_by(Message.created_at)
        return list((await s.execute(stmt)).scalars())


async def _archived(run_id: UUID, kind: EventType | None = None) -> list[RunEvent]:
    async with get_sessionmaker()() as s:
        stmt = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.thread_seq)
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


async def _bare_queued_run() -> UUID:
    """一个排队中的 run（不经 API —— 只测仓库层的领取语义）。"""
    async with get_sessionmaker()() as s:
        run = Run(
            id=uuid4(),
            thread_id=uuid4(),
            agent_version_id=uuid4(),
            status="queued",
            created_at=datetime(2000, 1, 1, tzinfo=UTC),  # 排在所有残留 run 前面
        )
        s.add(run)
        await s.commit()
        return run.id


async def _claim_until(
    runs: RunRepository, run_id: UUID, owner: str, lease: float
) -> str | bool | None:
    """领取直到领到 run_id（库里可能有别的测试残留的排队 run）。没领到它返回 False。"""
    while (got := await runs.claim_next(owner=owner, lease_s=lease)) is not None:
        if got[0] == run_id:
            return got[1]
    return False
