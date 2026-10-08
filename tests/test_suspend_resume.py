"""一轮分多段执行：委派挂起 → 子 run 跑完 → 续跑，全程是**同一个 run**。

这是长委派的立身之本。一次委派可以跑一小时，而父 run 在进程里等那么久的
代价是明确的：部署一次、OOM 一次，父 run 就没了，连带丢掉子 run 已经干完
的活。挂起把等待交给数据库。

对外看起来仍然是一个 run —— 同一个 run_id、同一条事件流、同一份用量。
这里测的就是这条「对外不变」以及它背后几个容易漏的不变量：

  · 事件 seq 跨段连续（前端靠它去重与补齐）
  · 用量跨段累加（覆盖的话账面只剩最后一段）
  · 续跑时模型看到的是**真实结论**，不是哨兵
  · 会话锁在挂起期间不松手
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_server.config import get_settings
from atlas_server.db.models import Message, Run
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select

from .fakes import TurnModel, tool_call_chunk

pytestmark = pytest.mark.usefixtures("clean_db")

PARENT_MODEL = "claude-sonnet-5"
CHILD_MODEL = "claude-haiku-4-5"

#: 让委派必然走挂起：当场等待窗口压到 0，第一次轮询就转异步。
#:
#: ★ 这正是生产里 subagent_inline_wait_s 的作用 —— 它是改造的风险闸门，
#:   短委派走老路径、长委派走挂起。测试把闸门开到最大来盯住新路径。
INSTANT_SUSPEND = 0.0


def _task_call(brief: str, name: str, call_id: str) -> AIMessageChunk:
    args = json.dumps({"description": brief, "subagent_type": name}, ensure_ascii=False)
    return tool_call_chunk("task", args, call_id)


def make_app(parent: TurnModel, child: TurnModel, *, inline_wait: float):
    app = create_app()
    settings = get_settings().model_copy(update={"subagent_inline_wait_s": inline_wait})
    models = {PARENT_MODEL: parent, CHILD_MODEL: child}

    def builder(model_spec, **_kw):
        # 标题/摘要模型不在脚本里 —— 回落到子模型，它们的输出本测试不看。
        return models.get(model_spec.model, child)

    app.state.executor = InProcessExecutor(
        get_sessionmaker(), settings, model_builder=builder
    )
    return app


@pytest.fixture
async def client_factory():
    clients: list[httpx.AsyncClient] = []
    executors: list[InProcessExecutor] = []

    async def make(
        parent: TurnModel, child: TurnModel, *, inline_wait: float = INSTANT_SUSPEND
    ) -> tuple[httpx.AsyncClient, InProcessExecutor]:
        app = make_app(parent, child, inline_wait=inline_wait)
        c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        executors.append(app.state.executor)
        return c, app.state.executor

    yield make
    for executor in executors:
        await executor.shutdown()
    for c in clients:
        await c.aclose()


async def setup_thread(client: httpx.AsyncClient) -> str:
    agent = await client.post(
        "/v1/agents",
        json={
            "slug": "delegator",
            "name": "委派者",
            "spec": {
                "system_prompt": "你会委派任务。",
                "model": {"model": PARENT_MODEL},
                "tool_names": ["task"],
                "subagents": [
                    {
                        "name": "coder",
                        "description": "写代码",
                        "system_prompt": "你是 coder。",
                        "model": {"model": CHILD_MODEL},
                        "session_mode": "persistent",
                    }
                ],
            },
        },
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    return thread.json()["id"]


async def start_run(client: httpx.AsyncClient, thread_id: str, prompt: str) -> str:
    accepted = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": prompt}]},
    )
    assert accepted.status_code == 202, accepted.text
    return accepted.json()["run_id"]


async def wait_status(
    client: httpx.AsyncClient, run_id: str, wanted: set[str], *, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        last = (await client.get(f"/v1/runs/{run_id}")).json()
        if last.get("status") in wanted:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 未进入 {wanted}，停在 {last.get('status')!r}")


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def _delegating_parent() -> TurnModel:
    return TurnModel(
        scripts=[
            [_task_call("写个排序函数", "coder", "c1")],
            [AIMessageChunk(content="coder 说结论是 42，我照着回答。")],
        ]
    )


async def events_of(run_id: str) -> list:
    from atlas_server.db.models import RunEvent

    async with get_sessionmaker()() as session:
        stmt = (
            select(RunEvent).where(RunEvent.run_id == UUID(run_id)).order_by(RunEvent.seq)
        )
        return list((await session.execute(stmt)).scalars())


# ──────────────────────────────────────────────── 全程一个 run


async def test_a_long_delegation_becomes_one_run_in_two_segments(client_factory) -> None:
    """★ 核心性质：挂起再续跑，对外仍是**一个** run，最终照常 succeeded。"""
    parent = _delegating_parent()
    child = TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    client, _ = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")

    final = await wait_status(client, run_id, TERMINAL)
    assert final["status"] == "succeeded", final

    # 父模型被调了两次：第一段发委派，第二段拿着结论收尾。
    assert parent.call_count == 2

    # 只有这一个用户发起的 run —— 没有为了续跑而新建 run。
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(Run).where(
                        Run.thread_id == UUID(thread_id), Run.parent_run_id.is_(None)
                    )
                )
            ).scalars()
        )
    assert [str(r.id) for r in rows] == [run_id]


async def test_the_run_passes_through_suspended(client_factory) -> None:
    """挂起是可观测的状态，不是一闪而过的内部细节。

    用户会盯着这个状态等一小时 —— 它必须能被查到，否则界面只能显示
    「运行中」而无法区分「在等子智能体」和「卡住了」。
    """
    parent = _delegating_parent()
    child = TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    client, _executor = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)

    types = [e.type for e in await events_of(run_id)]
    assert "run.suspended" in types, types
    # 挂起**不是**终止事件：它后面还跟着这一轮真正的结束
    assert types.index("run.suspended") < types.index("run.finished")


async def test_event_seq_keeps_climbing_across_segments(client_factory) -> None:
    """★ seq 跨段接着数，不是每段从 1 重来。

    每段各自从 1 数的话，前端断线重连会拿到错乱的历史 —— 它靠 seq 去重，
    第二段的 seq=1 会被当成早已收到的那条丢掉。

    ★ 这里断言的是**严格递增**而不是「无空洞」。归档表是事件流的一个
      **过滤后的子集** —— delta 类事件不落库（它们的价值是实时打字效果，
      回放时正文从 message.completed 一次到位）。所以空洞是预期的，而
      「无空洞」属于事件流那一层，由 run.last_seq 覆盖全部事件来保证。
    """
    client, _ = await client_factory(
        _delegating_parent(), TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    final = await wait_status(client, run_id, TERMINAL)

    events = await events_of(run_id)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs), seqs
    assert len(seqs) == len(set(seqs)), f"seq 有重复：{seqs}"
    assert seqs[0] >= 1

    # last_seq 数的是**全部**事件（含没归档的 delta），所以它 ≥ 归档的最大值
    assert final["last_seq"] >= seqs[-1]


async def test_thread_seq_is_assigned_and_ordered(client_factory) -> None:
    """★ 会话级序号必须被盖上 —— 它是归档表主键的一半。

    publish 的返回值是盖章后的那一份；丢掉返回值的表现是整批事件都撞在
    thread_seq=0 上，归档 INSERT 主键冲突，整段收尾失败而 run 卡在 running。
    """
    client, _ = await client_factory(
        _delegating_parent(), TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)

    events = await events_of(run_id)
    tseqs = [e.thread_seq for e in events]
    assert all(t > 0 for t in tseqs), f"有事件没盖会话序号：{tseqs}"
    assert tseqs == sorted(tseqs)
    assert len(tseqs) == len(set(tseqs))


async def test_deltas_are_not_archived(client_factory) -> None:
    """★ delta 不进归档表。

    一次回答几百到几千条 delta，加上子 run 的还要翻倍 —— 全落库的话一个长
    会话几十万行，而其中绝大多数永远不会被读到（前端读历史走 message 表）。
    """
    client, _ = await client_factory(
        _delegating_parent(), TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)

    types = [e.type for e in await events_of(run_id)]
    assert "message.delta" not in types, types
    # 但正文本身照常可回放 —— message.completed 一次到位
    assert "message.completed" in types


async def test_sub_run_events_land_in_the_parent_thread(client_factory) -> None:
    """★ 子 run 的事件归档到**父会话**名下 —— 「一个会话一条流」的落地。

    归到子会话的话，回放父会话那条流时子智能体的全过程都不在里面，而那正是
    审批弹窗在委派挂起期间消失的根因。
    """
    from atlas_server.db.models import RunEvent

    client, _ = await client_factory(
        _delegating_parent(), TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)

    async with get_sessionmaker()() as session:
        children = list(
            (
                await session.execute(select(Run).where(Run.parent_run_id == UUID(run_id)))
            ).scalars()
        )
        assert children, "应当有子 run"
        rows = list(
            (
                await session.execute(
                    select(RunEvent).where(RunEvent.run_id == children[0].id)
                )
            ).scalars()
        )

    assert rows, "子 run 的事件没有归档"
    assert all(r.thread_id == UUID(thread_id) for r in rows), (
        "子 run 的事件归到了子会话名下，父会话的流里看不到它们"
    )
    # 而且带着 depth=1 —— 前端据此渲染进子智能体卡片（S1）
    assert all(r.depth == 1 for r in rows), [r.depth for r in rows]


async def test_usage_accumulates_across_segments(client_factory) -> None:
    """用量是两段之和，不是最后一段。

    覆盖写的话账面上只剩续跑那一段 —— 用户看到「这轮花了 2k」而实际烧了
    十倍，max_total_tokens 也随之形同虚设。
    """
    parent = TurnModel(
        scripts=[
            [
                _task_call("写个排序函数", "coder", "c1"),
                AIMessageChunk(
                    content="",
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                ),
            ],
            [
                AIMessageChunk(content="好了。"),
                AIMessageChunk(
                    content="",
                    usage_metadata={
                        "input_tokens": 200,
                        "output_tokens": 20,
                        "total_tokens": 220,
                    },
                ),
            ],
        ]
    )
    client, _ = await client_factory(
        parent, TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    final = await wait_status(client, run_id, TERMINAL)

    assert final["status"] == "succeeded", final
    assert final["total_tokens"] == 330, final


# ──────────────────────────────────────────────── 续跑看到的是真话


async def test_the_resumed_segment_sees_the_real_conclusion(client_factory) -> None:
    """★ 续跑时模型看到的是子智能体的结论，而不是哨兵。

    看到哨兵的后果是最坏的一种：它会把 `__ATLAS_SUSPENDED__:...`
    当成子智能体说的话，然后一本正经地向用户汇报一个不存在的结果。
    """
    seen: list[list[Any]] = []

    class Recorder(TurnModel):
        async def _astream(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(list(messages))
            async for chunk in super()._astream(messages, *args, **kwargs):
                yield chunk

    parent = Recorder(
        scripts=[
            [_task_call("写个排序函数", "coder", "c1")],
            [AIMessageChunk(content="好了。")],
        ]
    )
    client, _ = await client_factory(
        parent, TurnModel(scripts=[[AIMessageChunk(content="排序函数写好了，用的是快排")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)

    assert len(seen) == 2, "父模型应当被调用两段"
    second = json.dumps([m.content for m in seen[1]], ensure_ascii=False, default=str)
    assert "__ATLAS_SUSPENDED__" not in second
    assert "快排" in second, "续跑段必须看到子智能体的真实结论"


async def test_the_sentinel_stays_in_the_stored_history(client_factory) -> None:
    """★ 表里留着哨兵是**对的** —— 它忠实记录「这一段是在等待中结束的」。

    结论的权威住在子 run 自己的会话里，续跑时现取。两边都写一份就有了两个
    事实源，而它们会在子 run 被重跑（fresh=true）时分叉。
    """
    client, _ = await client_factory(
        _delegating_parent(), TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)

    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.thread_id == UUID(thread_id))
                    .order_by(Message.created_at)
                )
            ).scalars()
        )
    blob = json.dumps([r.content for r in rows], ensure_ascii=False, default=str)
    assert "__ATLAS_SUSPENDED__" in blob

    # 但它对用户不可见
    body = (await client.get(f"/v1/threads/{thread_id}/messages")).json()
    assert "__ATLAS_SUSPENDED__" not in json.dumps(body, ensure_ascii=False)


# ──────────────────────────────────────────────── 不被误当成孤儿


async def _freeze_as_suspended(run_id: str, *, child_status: str) -> None:
    """把一个已经跑完的 run 按回「挂起中」的状态。

    ★ 为什么是**构造**状态而不是去抓那个时间窗。想让子智能体「永远不返回」
      在假模型上做不到 —— 空内容也是一次正常完成，子 run 照样终态、照样
      来唤醒父 run（踩过：断言拿到的是 'queued'，因为唤醒已经发生了）。
      而真去拖时间会让测试既慢又不稳。

      「挂起中」本来就只是几个列的值。直接把它摆出来，测的反而是这些测试
      真正关心的东西：取消怎么收尾、扫描捞不捞、孤儿回收碰不碰它。
    """
    async with get_sessionmaker()() as session:
        await session.execute(
            Run.__table__.update()
            .where(Run.id == UUID(run_id))
            .values(status="suspended", finished_at=None)
        )
        await session.execute(
            Run.__table__.update()
            .where(Run.parent_run_id == UUID(run_id))
            .values(status=child_status)
        )
        await session.commit()


async def _suspend_a_run(
    client_factory, *, child_status: str = "running"
) -> tuple[httpx.AsyncClient, Any, str, str]:
    """跑完一轮，再把它按回挂起中。返回 (client, executor, thread_id, run_id)。

    child_status='running' = 子 run 还在干活（不该被续跑）；
    'succeeded' = 子 run 其实已经好了，只是唤醒信号丢了。
    """
    client, executor = await client_factory(
        _delegating_parent(), TurnModel(scripts=[[AIMessageChunk(content="结论是 42")]])
    )
    thread_id = await setup_thread(client)
    run_id = await start_run(client, thread_id, "帮我写个排序函数")
    await wait_status(client, run_id, TERMINAL)
    await _freeze_as_suspended(run_id, child_status=child_status)
    return client, executor, thread_id, run_id


async def test_the_sweeper_recovers_a_lost_wakeup(client_factory) -> None:
    """★ 唤醒信号丢了之后，只有这个扫描能把 run 捞回来。

    正常路径是子 run 收尾时主动叫醒父 run。但进程可能正好在那一刻被
    SIGKILL —— 信号没了，而父 run **不在** active_runs 里（那是故意的：
    suspended 没有进程是正常的），所以 reap_orphans 也不会管它。
    没有这个扫描，它会永远停在「等待子智能体」：没有报错、没有事件，
    只是一个永远转圈的界面。
    """
    # 子 run 其实已经跑完了，只是那声「我好了」没送到
    client, executor, _thread_id, run_id = await _suspend_a_run(
        client_factory, child_status="succeeded"
    )

    assert await executor.sweep_suspended() == 1
    final = await wait_status(client, run_id, TERMINAL)
    assert final["status"] == "succeeded", final


async def test_the_sweeper_leaves_runs_whose_children_are_still_working(
    client_factory,
) -> None:
    """子 run 还在跑就不能续 —— 续了模型会拿到一句「尚未结束」当结论。"""
    _client, executor, _thread_id, _run_id = await _suspend_a_run(client_factory)
    assert await executor.sweep_suspended() == 0


async def test_cancelling_a_suspended_run_settles_it(client_factory) -> None:
    """★ 在等待中被取消的 run 必须自己收干净。

    别处的取消都发生在**有进程在跑**的时候 —— runner 的循环发出
    run.cancelled 然后走正常收尾。挂起时没有任何进程，不自己收的话这个 run
    会一直躺在 suspended，会话也一直被它占着（用户于是连新消息都发不出）。
    """
    client, _executor, thread_id, run_id = await _suspend_a_run(client_factory)

    cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text

    final = await wait_status(client, run_id, TERMINAL)
    assert final["status"] == "cancelled", final

    # 会话随之解锁 —— 用户能接着说话
    again = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": "换个思路"}]},
    )
    assert again.status_code == 202, again.text


async def test_cancelling_the_parent_cascades_to_the_child(client_factory) -> None:
    """★ 父被取消时子 run 也要停。

    父挂起时**没有进程**盯着子 run —— 原先那条「父的轮询循环顺手取消子 run」
    的路径不存在了。不级联的话子 run 会一路跑到自己的超时，而 acp 的子 run
    整段时间都占着一个 Pod；更难解释的是它跑完后还会来唤醒一个已被取消的
    父 run。
    """
    from atlas_server.redisx import make_redis
    from atlas_server.stream.relay import EventRelay

    client, _executor, _thread_id, run_id = await _suspend_a_run(client_factory)
    async with get_sessionmaker()() as session:
        children = list(
            (
                await session.execute(select(Run).where(Run.parent_run_id == UUID(run_id)))
            ).scalars()
        )
    assert children

    await client.post(f"/v1/runs/{run_id}/cancel")

    redis = make_redis(get_settings())
    try:
        relay = EventRelay(redis)
        for child in children:
            assert await relay.is_cancelled(child.id), f"子 run {child.id} 没有被级联取消"
    finally:
        await redis.aclose()


async def test_an_approval_during_suspension_reaches_the_thread_stream(
    client_factory,
) -> None:
    """★ S5 的验收点：**父 run 挂起期间**，子智能体的审批照样能被看见。

    这正是旧机制失效的那个场景。原先靠 SubagentService 在轮询循环里把子 run
    的待审批冒泡到父流，而挂起之后：

      ① 轮询循环已经结束（_await_result 返回哨兵就退出了）
      ② 图也已跳出，get_stream_writer() 拿不到出口
      ③ 父流上再不会有任何新事件

    三处同时失效，于是弹窗永远不出现，子智能体等到 approval_timeout_s 过期
    （等同拒绝），用户等了很久拿到一句「我没有权限做 X」。

    订阅单位换成会话之后这个问题由架构消解：子 run 的事件本来就在同一条流上。
    这条测试钉住的就是「挂起中的 run，其子 run 的事件仍然进得来」。
    """
    from atlas_server.domain.events import EventFactory, EventType
    from atlas_server.redisx import make_redis
    from atlas_server.repositories.run import RunRepository
    from atlas_server.stream.relay import EventRelay, thread_stream_key

    _client, _executor, thread_id, run_id = await _suspend_a_run(client_factory)

    async with get_sessionmaker()() as session:
        children = list(
            (
                await session.execute(select(Run).where(Run.parent_run_id == UUID(run_id)))
            ).scalars()
        )
    assert children
    sub_run_id = children[0].id

    # 父 run 此刻是 suspended —— 没有任何进程在跑它
    async with get_sessionmaker()() as session:
        parent = await session.get(Run, UUID(run_id))
        assert parent is not None and parent.status == "suspended"

    # 子 run 请求审批：按自己的身份往**会话**流上发（base_depth=1）
    redis = make_redis(get_settings())
    try:
        relay = EventRelay(redis, ttl_s=get_settings().run_events_ttl_s)
        async with get_sessionmaker()() as session:
            floor = await RunRepository(session).thread_seq_floor(UUID(thread_id))
        event = EventFactory(sub_run_id, _utcnow, base_depth=1).make(
            EventType.APPROVAL_REQUIRED,
            {"approval_id": str(uuid4()), "tool_name": "shell", "args": {}, "reason": "需要确认"},
        )
        stamped = await relay.publish(event, thread_id=UUID(thread_id), floor=floor)

        # ★ 关键断言：它真的进了**父会话**那条流 —— 前端订阅的就是它
        entries = await redis.xrange(thread_stream_key(UUID(thread_id)))
    finally:
        await redis.aclose()

    payloads = [f["payload"] for _id, f in entries]
    assert any("approval.required" in p for p in payloads), "审批事件没进会话流"
    assert stamped.thread_seq > floor, "会话序号没往前走"
    assert stamped.depth == 1, "没标成子智能体的事件"


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def test_a_suspended_run_still_holds_the_thread(client_factory) -> None:
    """★ 挂起期间会话仍被占用 —— 否则两个 run 会交错写同一个 thread。

    这条承诺原先由 Redis 锁的 TTL 兜着，而挂起可以持续一小时 —— 没有任何
    TTL 覆盖得了它，且挂起期间没有进程替它续租。权威判据因此搬到了 DB：
    「这个会话上还有没有没跑完的 run」。
    """
    client, _executor, thread_id, _run_id = await _suspend_a_run(client_factory)

    second = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": "再来一句"}]},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["kind"] == "thread_locked"
