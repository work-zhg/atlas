"""子会话与子 run —— 委派的端到端行为。

假模型 + 真实的执行链路：POST /runs → 主 run 发 `task` → SubagentService
建子会话、起子 run → 子 run 走与普通 run **完全同一条**链路 → 最终文本
回到主 run。整条链路脱离网络运行。

设计验收点（detail/subagent.html §11 步骤 2）：
  · 连续委派两次，第二次的子 run 历史里能看到第一次的对话
  · 主线程的 message 表**不含**子智能体的任何消息
  · 子会话不出现在会话列表
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from atlas_server.config import get_settings
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk
from atlas_server.db.models import Message, Run, RunEvent, Thread
from sqlalchemy import select

from tests.fakes import TurnModel, tool_call_chunk

pytestmark = pytest.mark.usefixtures("clean_db")

PARENT_MODEL = "claude-opus-5"
CHILD_MODEL = "claude-haiku-4-5"


def _task_call(brief: str, name: str, call_id: str, *, fresh: bool = False) -> AIMessageChunk:
    args = json.dumps(
        {"description": brief, "subagent_type": name, "fresh": fresh}, ensure_ascii=False
    )
    return tool_call_chunk("task", args, call_id)


def make_app(parent: TurnModel, child: TurnModel):
    """按 ModelSpec.model 分发假模型 —— 父与子因此有各自的脚本。

    ★ 用同一个 TurnModel 会让父子共享调用计数：子 run 的第一轮会吃掉父的
      第二段脚本，测出来的东西与真实行为无关。
    """
    app = create_app()
    models = {PARENT_MODEL: parent, CHILD_MODEL: child}

    def builder(model_spec, **_kw):
        return models[model_spec.model]

    app.state.executor = InProcessExecutor(
        get_sessionmaker(), get_settings(), model_builder=builder
    )
    return app


@pytest.fixture
async def client_factory():
    clients: list[httpx.AsyncClient] = []

    async def make(parent: TurnModel, child: TurnModel) -> httpx.AsyncClient:
        transport = ASGITransport(app=make_app(parent, child))
        c = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(c)
        return c

    yield make
    for c in clients:
        await c.aclose()


async def setup_thread(client: httpx.AsyncClient, *, session_mode: str = "persistent") -> str:
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
                        "session_mode": session_mode,
                    }
                ],
            },
        },
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    return thread.json()["id"]


async def run_turn(client: httpx.AsyncClient, thread_id: str, prompt: str) -> dict:
    accepted = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": prompt}]},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]

    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = asyncio.get_running_loop().time() + 20.0
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in terminal:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 超时未结束")


async def sub_threads(parent_id: str) -> list[Thread]:
    """父会话下的子会话，按创建时间正序。

    ★ 走 ORM 而不是裸 SQL：UUID 列在 MySQL 上存的是去横杠的 CHAR(32)，
      拿 API 返回的带横杠字符串去裸查会静默返回空集 —— 断言于是变成
      「什么都没发生」，而真实行为可能完全正确。
    """
    async with get_sessionmaker()() as session:
        stmt = (
            select(Thread)
            .where(Thread.parent_thread_id == UUID(parent_id))
            .order_by(Thread.created_at, Thread.id)
        )
        return list((await session.execute(stmt)).scalars())


async def child_runs() -> list[Run]:
    async with get_sessionmaker()() as session:
        stmt = select(Run).where(Run.parent_run_id.isnot(None))
        return list((await session.execute(stmt)).scalars())


async def runs_of(thread_id) -> list[Run]:
    async with get_sessionmaker()() as session:
        stmt = select(Run).where(Run.thread_id == thread_id)
        return list((await session.execute(stmt)).scalars())


async def messages_blob(thread_id, *, role: str | None = None) -> str:
    """一段会话的全部消息内容，拼成一个字符串供包含性断言。"""
    async with get_sessionmaker()() as session:
        stmt = select(Message).where(Message.thread_id == thread_id)
        if role:
            stmt = stmt.where(Message.role == role)
        rows = list((await session.execute(stmt.order_by(Message.created_at))).scalars())
    return json.dumps([r.content for r in rows], ensure_ascii=False, default=str)


# ──────────────────────────────────────────────── 首次委派


async def test_delegation_creates_a_child_thread_and_child_run(client_factory) -> None:
    """一次委派 = 一个子会话 + 一个指回发起者的子 run。

    ★ 这条红了说明委派又退回成「图内跑子图」—— 于是独立事件流、独立审批、
      独立取消、孤儿回收这四样东西一起没了（设计 §06）。
    """
    parent = TurnModel(
        scripts=[
            [_task_call("写个排序函数", "coder", "c1")],
            [AIMessageChunk(content="coder 写完了。")],
        ]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="def sort(xs): return sorted(xs)")]])
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    result = await run_turn(client, thread_id, "帮我写个排序函数")
    assert result["status"] == "succeeded", result

    subs = await sub_threads(thread_id)
    assert len(subs) == 1
    assert subs[0].subagent_name == "coder"
    assert subs[0].status == "active"
    assert subs[0].workspace_thread_id == UUID(thread_id)  # 挂父的工作区

    children = await child_runs()
    assert len(children) == 1
    assert children[0].thread_id == subs[0].id
    assert children[0].parent_run_id == UUID(result["id"])
    assert children[0].status == "succeeded"


async def test_subagent_messages_never_touch_the_main_thread(client_factory) -> None:
    """主线程只看见结论，看不见子智能体的中间过程。

    「独立上下文」落到持久化上就是这一条：委派的价值之一正是不让子任务的
    中间过程污染主对话（设计 §03）。
    """
    parent = TurnModel(
        scripts=[
            [_task_call("写个排序函数", "coder", "c1")],
            [AIMessageChunk(content="coder 写完了。")],
        ]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="子智能体的长篇中间过程")]])
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    await run_turn(client, thread_id, "帮我写个排序函数")

    blob = await messages_blob(UUID(thread_id))
    assert "子智能体的长篇中间过程" not in blob
    assert "coder 写完了。" in blob

    # 子会话那边则完整保留：任务书 + 子智能体的回答
    sub_blob = await messages_blob((await sub_threads(thread_id))[0].id)
    assert "写个排序函数" in sub_blob
    assert "子智能体的长篇中间过程" in sub_blob


async def test_child_threads_are_not_in_the_users_thread_list(client_factory) -> None:
    """子会话是父会话的一部分，不是用户的一段独立对话。"""
    parent = TurnModel(
        scripts=[[_task_call("干活", "coder", "c1")], [AIMessageChunk(content="好了")]]
    )
    client = await client_factory(parent, TurnModel(scripts=[[AIMessageChunk(content="ok")]]))

    thread_id = await setup_thread(client)
    await run_turn(client, thread_id, "干活")

    listed = (await client.get("/v1/threads")).json()["data"]
    assert [t["id"] for t in listed] == [thread_id]
    # 但直接按 id 取得到，且能看出自己的身份
    sub_id = (await sub_threads(thread_id))[0].id
    detail = (await client.get(f"/v1/threads/{sub_id}")).json()
    assert detail["subagent_name"] == "coder"
    assert detail["parent_thread_id"] == thread_id


# ──────────────────────────────────────────────── 第二次委派：恢复


async def test_second_delegation_resumes_the_same_session(client_factory) -> None:
    """★ 本次改造的核心验收点：第二次委派看得见第一次的对话。

    子会话只有一个（不是两个），且子智能体第二轮的输入里含有第一轮的历史 ——
    「独立上下文」的含义从「每次都空」变成了「与主线程分开、但自身连续」。
    """
    seen_histories: list[int] = []

    class RecordingChild(TurnModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            seen_histories.append(len(messages))
            async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
                yield chunk

    parent = TurnModel(
        scripts=[
            [_task_call("第一步：读代码", "coder", "c1")],
            [AIMessageChunk(content="第一步完成")],
            [_task_call("第二步：改那三处", "coder", "c2")],
            [AIMessageChunk(content="第二步完成")],
        ]
    )
    child = RecordingChild(
        scripts=[
            [AIMessageChunk(content="读完了，有三处要改")],
            [AIMessageChunk(content="三处都改好了")],
        ]
    )
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    assert (await run_turn(client, thread_id, "开始"))["status"] == "succeeded"
    assert (await run_turn(client, thread_id, "继续"))["status"] == "succeeded"

    # 一个子会话，两个子 run —— 而不是两个子会话
    subs = await sub_threads(thread_id)
    assert len(subs) == 1
    assert len(await runs_of(subs[0].id)) == 2

    # 第二轮的输入更长 —— 历史确实被重建进去了
    assert len(seen_histories) == 2
    assert seen_histories[1] > seen_histories[0], seen_histories


async def test_fresh_archives_the_old_session_instead_of_deleting_it(client_factory) -> None:
    """`fresh=True` 是「上一轮把它带偏了，重来」的出口。

    ★ 归档而不是删：历史不该因为一次重来就消失（设计 §04）。旧会话必须
      让出查找键，否则下一次委派会撞唯一约束。
    """
    parent = TurnModel(
        scripts=[
            [_task_call("第一次", "coder", "c1")],
            [AIMessageChunk(content="一轮完成")],
            [_task_call("重来", "coder", "c2", fresh=True)],
            [AIMessageChunk(content="二轮完成")],
        ]
    )
    child = TurnModel(
        scripts=[[AIMessageChunk(content="结论 A")], [AIMessageChunk(content="结论 B")]]
    )
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    await run_turn(client, thread_id, "开始")
    await run_turn(client, thread_id, "重来")

    subs = await sub_threads(thread_id)
    assert len(subs) == 2, subs
    assert subs[0].status == "archived"  # 旧的留着，只是归档
    assert subs[1].status == "active"
    # subagent_name 保留：执行器要靠它派生子智能体的 spec
    assert [t.subagent_name for t in subs] == ["coder", "coder"]


# ──────────────────────────────────────────────── 一次性模式


async def test_ephemeral_subagent_gets_a_new_session_every_time(client_factory) -> None:
    """一次性子智能体每次新建会话，因此不占查找键、可以并行（设计 §09）。"""
    parent = TurnModel(
        scripts=[
            [_task_call("调研 A", "coder", "c1")],
            [AIMessageChunk(content="A 完成")],
            [_task_call("调研 B", "coder", "c2")],
            [AIMessageChunk(content="B 完成")],
        ]
    )
    child = TurnModel(
        scripts=[[AIMessageChunk(content="A 的结论")], [AIMessageChunk(content="B 的结论")]]
    )
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client, session_mode="ephemeral")
    await run_turn(client, thread_id, "调研 A")
    await run_turn(client, thread_id, "调研 B")

    subs = await sub_threads(thread_id)
    assert len(subs) == 2
    # 两条都是 ephemeral：既不被「恢复」命中，也不占 (parent, name) 的唯一键
    assert {t.status for t in subs} == {"ephemeral"}


# ──────────────────────────────────────────────── 结论回传


async def test_the_final_text_reaches_the_parent_as_the_tool_result(client_factory) -> None:
    """子智能体的最终文本是 `task` 的返回值 —— 主 agent 据此继续。"""
    parent = TurnModel(
        scripts=[
            [_task_call("算一下", "coder", "c1")],
            [AIMessageChunk(content="转述完毕")],
        ]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="答案是 42")]])
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    run = await run_turn(client, thread_id, "算一下")
    assert run["status"] == "succeeded"

    # 权威来源是子会话的 message 表（事件在 Redis 里有 TTL）
    sub_id = (await sub_threads(thread_id))[0].id
    assert "答案是 42" in await messages_blob(sub_id, role="assistant")


async def test_child_run_uses_the_subagents_own_config(client_factory) -> None:
    """子 run 跑的是**子智能体的**配置，不是父 agent 的。

    `run.started` 是权威视角：它记的是这次 run 实际装出来的模型、工具与身份。
    两条断言各守一头 ——
      · agent_name/slug 是子智能体的 ⇒ spec 派生生效了
      · 工具里没有 `task` ⇒ 深度结构性封顶在 1，子智能体不能再委派
    """
    parent = TurnModel(
        scripts=[[_task_call("干活", "coder", "c1")], [AIMessageChunk(content="好了")]]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="ok")]])
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    await run_turn(client, thread_id, "干活")

    sub = (await sub_threads(thread_id))[0]
    child_run = (await runs_of(sub.id))[0]

    async with get_sessionmaker()() as session:
        stmt = (
            select(RunEvent)
            .where(RunEvent.run_id == child_run.id, RunEvent.type == "run.started")
            .limit(1)
        )
        started = (await session.execute(stmt)).scalars().one()

    assert started.data["agent_name"] == "coder"
    assert started.data["agent_slug"] == "delegator/coder"
    assert started.data["model"] == CHILD_MODEL
    assert "task" not in started.data["tools"]


async def test_admission_limits_are_global_and_per_run(client_factory, monkeypatch) -> None:
    """撞上限时模型拿到的是**可操作的错误**，不是超时。

    ★ 上限必须让模型知道原因和出路：acp 子智能体各吃一个 Pod，闸门是
      真会关上的，而「卡住直到超时」既浪费一整个 run 的时长，也让模型
      无从改派。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "subagent_max_per_run", 1, raising=False)

    parent = TurnModel(
        scripts=[
            [_task_call("第一次", "coder", "c1")],
            [_task_call("第二次", "coder", "c2")],
            [AIMessageChunk(content="收到上限提示，我自己来")],
        ]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="第一次的结论")]])
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    result = await run_turn(client, thread_id, "连着派两次")
    # run 不该因为委派被拒而失败 —— 这是可恢复的局部问题
    assert result["status"] == "succeeded", result

    # 只起了一个子 run；第二次被挡在准入这一步，连 run 行都没建
    assert len(await child_runs()) == 1

    async with get_sessionmaker()() as session:
        stmt = select(RunEvent).where(
            RunEvent.run_id == UUID(result["id"]), RunEvent.type == "subagent.finished"
        )
        finished = list((await session.execute(stmt)).scalars())
    blob = json.dumps([e.data for e in finished], ensure_ascii=False)
    assert "上限" in blob, blob


def _two_task_calls(name_a: str, name_b: str) -> AIMessageChunk:
    """一个 assistant turn 里并发发起两次委派。"""
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": "task",
                "args": json.dumps({"description": "第一半", "subagent_type": name_a}),
                "id": "p1",
                "index": 0,
            },
            {
                "name": "task",
                "args": json.dumps({"description": "第二半", "subagent_type": name_b}),
                "id": "p2",
                "index": 1,
            },
        ],
    )


async def test_same_subagent_twice_in_one_turn_is_rejected(client_factory) -> None:
    """同一轮里对同一个子智能体发两个 `task`，第二个拿到可操作的错误。

    ★ 这条约束不是新发明的规则：子会话也是 thread，会话串行锁原样适用。
      工具层的拦截只是把一个**必然发生**的冲突提前到调用时暴露 ——
      否则模型要等到锁超时才知道，既不知道原因也拿不到替代方案。
    """
    parent = TurnModel(
        scripts=[
            [_two_task_calls("coder", "coder")],
            [AIMessageChunk(content="一个成了，另一个改天")],
        ]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="做完了")]])
    client = await client_factory(parent, child)

    thread_id = await setup_thread(client)
    result = await run_turn(client, thread_id, "并发派两次")
    assert result["status"] == "succeeded", result

    # 只起了一个子 run —— 第二次连 run 行都没建
    assert len(await child_runs()) == 1
    assert len(await sub_threads(thread_id)) == 1

    async with get_sessionmaker()() as session:
        stmt = select(RunEvent).where(
            RunEvent.run_id == UUID(result["id"]), RunEvent.type == "subagent.finished"
        )
        blob = json.dumps(
            [e.data for e in (await session.execute(stmt)).scalars()], ensure_ascii=False
        )
    # 错误必须给出出路，不只是「被拒绝」
    assert "串行" in blob and ("合并" in blob or "不同的子智能体" in blob), blob


async def test_different_subagents_in_one_turn_run_in_parallel(client_factory) -> None:
    """不同子智能体 = 不同 thread，互不争锁，可以真并行（设计 §07）。"""
    parent = TurnModel(
        scripts=[
            [_two_task_calls("coder", "researcher")],
            [AIMessageChunk(content="两边都回来了")],
        ]
    )
    child = TurnModel(scripts=[[AIMessageChunk(content="做完了")]])
    client = await client_factory(parent, child)

    agent = await client.post(
        "/v1/agents",
        json={
            "slug": "two-subagents",
            "name": "双子",
            "spec": {
                "system_prompt": "p",
                "model": {"model": PARENT_MODEL},
                "tool_names": ["task"],
                "subagents": [
                    {
                        "name": n,
                        "description": n,
                        "system_prompt": n,
                        "model": {"model": CHILD_MODEL},
                    }
                    for n in ("coder", "researcher")
                ],
            },
        },
    )
    assert agent.status_code == 201, agent.text
    thread_id = (
        await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    ).json()["id"]

    result = await run_turn(client, thread_id, "并发派给两个人")
    assert result["status"] == "succeeded", result

    subs = await sub_threads(thread_id)
    assert sorted(t.subagent_name for t in subs) == ["coder", "researcher"]
    assert len(await child_runs()) == 2


async def test_thread_detail_reports_how_many_subsessions_would_be_deleted(
    client_factory,
) -> None:
    """删除确认要能说清影响面。

    ★ 删父会话会级联删掉子会话的 message / run / run_event —— 改造之后
      「删一个会话」的影响面变大了，界面上必须说清楚（设计 §12）。
    """
    parent = TurnModel(
        scripts=[[_task_call("干活", "coder", "c1")], [AIMessageChunk(content="好了")]]
    )
    client = await client_factory(parent, TurnModel(scripts=[[AIMessageChunk(content="ok")]]))

    thread_id = await setup_thread(client)
    before = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert before["subagent_thread_count"] == 0

    await run_turn(client, thread_id, "干活")

    after = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert after["subagent_thread_count"] == 1


async def test_thread_detail_exposes_the_running_run(client_factory) -> None:
    """★ 刷新页面要能接回在跑的 run —— 没有这个字段就是白屏。

    前端的 activeRunId 只在「发消息成功」时赋值，重新挂载后是 undefined，
    于是不订阅任何流；而助手消息要到 message.completed 才落库，长 run 期间
    message 表里只有用户那条提问。用户看到的就是「回答到一半没了，刷新后
    什么都没有」—— 实测一次委派卡满 300s，整段期间刷新都是空的。

    续传机制（Last-Event-ID + run_event 归档）早就有，缺的只是这个入口。
    """
    parent = TurnModel(scripts=[[AIMessageChunk(content="做完了")]])
    client = await client_factory(parent, TurnModel(scripts=[[AIMessageChunk(content="ok")]]))
    thread_id = await setup_thread(client)

    # 没跑任何东西时是空的
    assert (await client.get(f"/v1/threads/{thread_id}")).json()["active_run_id"] is None

    run_id = (await run_turn(client, thread_id, "干活"))["id"]  # run_turn 返回整个 body

    # 跑完之后也要回到空 —— 否则刷新会去订阅一条已经结束的流
    detail = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert detail["active_run_id"] is None, detail

    # 人为把它置回「在跑」，确认查得到的是这个 run
    async with get_sessionmaker()() as session:
        run = await session.get(Run, UUID(run_id))
        assert run is not None
        run.status = "awaiting_approval"  # 等审批在用户眼里也是「还在跑」
        await session.commit()

    detail = (await client.get(f"/v1/threads/{thread_id}")).json()
    assert detail["active_run_id"] == str(run_id)


async def test_subagent_approvals_are_forwarded_to_the_parent_stream(
    client_factory, monkeypatch
) -> None:
    """★ 子 run 的审批必须冒泡到**父**的事件流，否则是死锁。

    子 run 有自己的事件流，而前端只订阅父 run 那一条 —— 不冒泡的话弹窗
    永远不出现，子智能体一直等人点头，直到 bridge 的 adapter 超时。真实
    委派会话上实测：两次委派各卡满 300s，审批最终 expired，用户看到的是
    「回答一半就中断」。

    data 里必须带 run_id：决策要 POST 到**子** run 的端点，而父流上其它
    审批属于父 run。
    """
    from uuid import uuid4

    from atlas_server.db.models import Approval
    from atlas_server.services import subagent as subagent_mod

    written: list[dict] = []
    monkeypatch.setattr(subagent_mod, "get_stream_writer", lambda: written.append)

    parent = TurnModel(scripts=[[AIMessageChunk(content="占位")]])
    client = await client_factory(parent, TurnModel(scripts=[[AIMessageChunk(content="ok")]]))
    thread_id = await setup_thread(client)
    run_id = UUID((await run_turn(client, thread_id, "干活"))["id"])

    # 造一条「子 run 正在等审批」
    approval_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Approval(
                id=approval_id,
                run_id=run_id,
                tool_name="Write /workspace/report.md",
                args={"file_path": "/workspace/report.md"},
                status="pending",
            )
        )
        await session.commit()

    service = subagent_mod.SubagentService.__new__(subagent_mod.SubagentService)
    forwarded: set[UUID] = set()
    async with get_sessionmaker()() as session:
        await service._forward_approvals(session, "coder", run_id, forwarded)
        # 第二次不该重复发 —— 轮询每 0.5s 一次，重复会刷出一串同样的弹窗
        await service._forward_approvals(session, "coder", run_id, forwarded)

    assert len(written) == 1, written
    event = written[0]
    assert event["kind"] == "approval.required"
    assert event["approval_id"] == str(approval_id)
    assert event["tool_name"] == "Write /workspace/report.md"
    # ★ 这一条是关键：没有它，前端会把决策提交到父 run 的端点
    assert event["run_id"] == str(run_id)
