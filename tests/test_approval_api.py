"""P7 · 人工确认的 server 侧链路（文档 §12.2）。

engine 侧的时序与决策语义见 test_approval.py。
这里测的是「HTTP 决策能不能唤醒**挂起的** run」—— 整条链路的接缝。

★ 断言一个字没改（S7）：审批从「在线阻塞」改成「挂起」之后，外部行为完全
  一致 —— run 落成 awaiting_approval、决策后续跑到 succeeded、拒绝不终止、
  重复决策 409。变的只是内部机制：没有进程在等，唤醒由端点触发。

★ 原先有一条 test_decision_arriving_first_is_not_lost 论证「用 BLPOP 而非
  pub/sub」。那个队列现在没有消费者，整条测试随机制一起删掉了。
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
from atlas_server.config import get_settings
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk

from tests.fakes import TurnModel, tool_call_chunk
from tests.test_runs_api import wait_for_status

GUARDED_SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": ["write_todos"],
    "limits": {"require_approval_for": ["write_todos"]},
}


def make_app():
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: TurnModel(
            scripts=[
                [tool_call_chunk("write_todos", '{"todos":[]}', "c1")],
                [AIMessageChunk(content="办完了")],
            ]
        ),
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def redis() -> aioredis.Redis:
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


async def _start_guarded_run(client: httpx.AsyncClient) -> tuple[str, str]:
    """发起一个会触发审批的 run，返回 (run_id, approval_id)。"""
    agent = await client.post(
        "/v1/agents", json={"slug": "guarded", "name": "受管控", "spec": GUARDED_SPEC}
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"], "title": "t"})
    res = await client.post(
        f"/v1/threads/{thread.json()['id']}/runs",
        json={"content": [{"type": "text", "text": "记个待办"}]},
    )
    run_id = res.json()["run_id"]

    # 轮询待决端点而不是从 SSE 里抠 —— 中途 break 出 aiter_lines 会在服务端
    # xread 阻塞里掐断生成器，那是测试写法造成的噪音，不是被测行为。
    for _ in range(60):
        pending = (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"]
        if pending:
            return run_id, pending[0]["id"]
        await asyncio.sleep(0.1)
    raise AssertionError("没等到待决的确认项")


@pytest.mark.usefixtures("clean_db")
async def test_run_waits_and_marks_awaiting_approval(client: httpx.AsyncClient) -> None:
    """★ 执行器真的停在那里等 —— 而不是继续往下跑。"""
    run_id, _ = await _start_guarded_run(client)

    await asyncio.sleep(0.3)
    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "awaiting_approval", f"实际 {run['status']}"


@pytest.mark.usefixtures("clean_db")
async def test_approve_resumes_the_run(client: httpx.AsyncClient) -> None:
    """★ 整条链路的接缝：HTTP 决策 → Redis → 阻塞中的执行器被唤醒。"""
    run_id, approval_id = await _start_guarded_run(client)

    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
    )
    assert res.status_code == 200, res.text

    run = await wait_for_status(client, run_id)
    assert run["status"] == "succeeded", run


@pytest.mark.usefixtures("clean_db")
async def test_reject_lets_the_run_finish(client: httpx.AsyncClient) -> None:
    """§12.2：拒绝不终止 run。"""
    run_id, approval_id = await _start_guarded_run(client)

    await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "rejected"})
    run = await wait_for_status(client, run_id)
    assert run["status"] == "succeeded", "拒绝把整轮弄失败了"


@pytest.mark.usefixtures("clean_db")
async def test_second_decision_conflicts(client: httpx.AsyncClient) -> None:
    """★ 多标签页 / 重复点击：只有第一次决策生效。"""
    run_id, approval_id = await _start_guarded_run(client)

    first = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
    )
    second = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "rejected"}
    )

    assert first.status_code == 200
    assert second.status_code == 409, "重复决策没被拒"
    await wait_for_status(client, run_id)


@pytest.mark.usefixtures("clean_db")
async def test_an_approval_survives_a_process_restart(client: httpx.AsyncClient) -> None:
    """★ S7 的立身之本：等审批的 run 挂起在**数据库**里，不在进程里。

    原先审批是一个阻塞在 BLPOP 上的 asyncio.Task —— 进程重启就全丢，而
    `approval_timeout_s=600` 对小时级任务根本不够：一个跑 40 分钟的 CLI 在第
    35 分钟弹审批，用户没盯着屏幕就按超时拒绝处理了。

    这里模拟一次完整的重启：把执行器换成一个全新的实例（进程内的所有 task
    随之作废），然后提交决策 —— run 照样能被唤醒并跑完。
    """
    run_id, approval_id = await _start_guarded_run(client)

    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "awaiting_approval", run

    # 模拟重启：旧执行器的 task 全部作废，换一个新的
    old = client._transport.app.state.executor  # type: ignore[attr-defined]
    await old.shutdown()
    fresh = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: TurnModel(
            scripts=[
                [tool_call_chunk("write_todos", '{"todos":[]}', "c1")],
                [AIMessageChunk(content="重启后办完了")],
            ]
        ),
    )
    client._transport.app.state.executor = fresh  # type: ignore[attr-defined]

    still = (await client.get(f"/v1/runs/{run_id}")).json()
    assert still["status"] == "awaiting_approval", f"被孤儿回收误杀了：{still}"

    # 决策到了 —— 新进程把它续起来
    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
    )
    assert res.status_code == 200, res.text

    final = await wait_for_status(client, run_id)
    assert final["status"] == "succeeded", final


@pytest.mark.usefixtures("clean_db")
async def test_a_pending_approval_does_not_resume_early(client: httpx.AsyncClient) -> None:
    """★ 没决策就不能续跑 —— 否则死循环。

    审批挂起时**没有子 run**。若 barrier 用委派那套判据（「子 run 都终态了」），
    它会立刻满足：续跑 → gate 还是 pending → 又挂起 → 再续跑，无休止。
    run.waiting_on 里的 reason 就是为了让 barrier 分派对（迁移 0012）。
    """
    run_id, _approval_id = await _start_guarded_run(client)
    executor = client._transport.app.state.executor  # type: ignore[attr-defined]

    assert await executor.resume_if_ready(run_id) is False, "没决策就被续跑了"
    assert await executor.sweep_suspended() == 0

    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "awaiting_approval", run


@pytest.mark.usefixtures("clean_db")
async def test_an_old_approval_never_expires(client: httpx.AsyncClient) -> None:
    """★ 审批只由人决定：等多久都不替用户按拒绝处理（原先一天后由扫描判死）。"""
    from datetime import UTC, datetime, timedelta

    from atlas_server.db.models import Approval
    from sqlalchemy import update

    run_id, approval_id = await _start_guarded_run(client)
    executor = client._transport.app.state.executor  # type: ignore[attr-defined]

    # 把它的创建时间推回到很久以前（模拟等了一个月）
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Approval).where(Approval.id == UUID(approval_id)).values(created_at=long_ago)
        )
        await session.commit()

    assert await executor.sweep_suspended() == 0
    assert (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "awaiting_approval"
    async with get_sessionmaker()() as session:
        row = await session.get(Approval, UUID(approval_id))
        assert row is not None and row.status == "pending", row.status if row else None

    # 人终于做了决定 —— 照样生效
    res = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
    )
    assert res.status_code == 200, res.text
    final = await wait_for_status(client, run_id)
    assert final["status"] == "succeeded", final
    thread_id = (await client.get(f"/v1/runs/{run_id}")).json()["thread_id"]
    again = await client.post(
        f"/v1/threads/{thread_id}/runs",
        json={"content": [{"type": "text", "text": "换个思路"}]},
    )
    assert again.status_code == 202, again.text


@pytest.mark.usefixtures("clean_db")
async def test_an_online_wait_is_not_mistaken_for_a_suspension(
    client: httpx.AsyncClient,
) -> None:
    """★ `awaiting_approval` 在两条路径上含义不同，扫描不能一视同仁。

        native   图已跳出、进程已结束 —— 真挂起，waiting_on 有值
        acp      CLI 在 Pod 里发起的同步 RPC，**run 还在跑** —— bridge 正等着
                 响应，只能在线等；这期间 gate 也把 run 标成 awaiting_approval，
                 但它**没有** waiting_on

    不区分的话扫描会把一个正在跑的 acp 子 run 当成可续跑并 submit 第二次 ——
    第二条上游连接 连上同一个 Pod，bridge 按 CLOSE_SUPERSEDED(4409) 关掉
    第一个，整轮 runtime_crashed。真机验证时撞到过，症状是「审批批准了，
    子 run 却崩了」。
    """
    from atlas_server.db.models import Run
    from sqlalchemy import update

    run_id, _approval_id = await _start_guarded_run(client)
    executor = client._transport.app.state.executor  # type: ignore[attr-defined]

    # 模拟 acp 的在线等待：状态是 awaiting_approval，但没有 waiting_on
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Run).where(Run.id == UUID(run_id)).values(waiting_on=None)
        )
        await session.commit()

    assert await executor.sweep_suspended() == 0, "在线等待的 run 被当成挂起续跑了"
    assert await executor.resume_if_ready(UUID(run_id)) is False

    async with get_sessionmaker()() as session:
        row = await session.get(Run, UUID(run_id))
        assert row is not None and row.status == "awaiting_approval"


@pytest.mark.usefixtures("clean_db")
async def test_a_very_long_tool_name_still_registers(
    client: httpx.AsyncClient, redis: aioredis.Redis
) -> None:
    """★ 长工具名必须能落库 —— 这是 2026-09-23 那次静默故障的根因。

    `Approval.tool_name` 是 varchar(128)。native 往里写的是真工具名（十几个
    字符），acp 写的是 CLI 给的 `toolCall.title` —— 而 Terminal 工具的 title
    **就是整条命令**，一条带几个 `&&` 的验证命令轻松过 128。

    后果不是一条报错，而是一次**静默拒绝**：`check()` 把落库异常按不放行处理，
    CLI 收到 reject_once 就停手，子 run 零文本产出，父模型只看到一句「子智能体
    没有产出文本结论」—— 从那里根本推不回列宽。真机上的表现是「短命令的审批
    全过，长命令的审批全挂」，而 approval 表里干脆没有那几行。

    截断放在仓储层，所以这条测试走的就是生产路径的那个 gate。
    """
    from atlas_server.db.models import Approval
    from atlas_server.repositories.approval import _TOOL_NAME_MAX
    from atlas_server.services.approval import RedisApprovalGate

    run_id, _ = await _start_guarded_run(client)

    long_name = "`" + " && ".join(f"cat /workspace/file-{i}.txt" for i in range(20)) + "`"
    assert len(long_name) > _TOOL_NAME_MAX, "这条测试的前提是名字真的超长"

    gate = RedisApprovalGate(get_sessionmaker(), redis, UUID(run_id))
    approval_id = str(uuid4())
    state = await gate.check(approval_id=approval_id, tool_name=long_name, args={"x": 1})

    assert state == "pending", f"长工具名没能登记，退化成了 {state!r}"

    async with get_sessionmaker()() as session:
        row = await session.get(Approval, UUID(approval_id))
        assert row is not None, "approval 没落库 —— 事务回滚了"
        assert len(row.tool_name) <= _TOOL_NAME_MAX
        # 截断保留开头：排查时一眼能认出是哪条命令
        assert row.tool_name.startswith("`cat /workspace/file-0.txt")
