"""P7 · 人工确认的 server 侧链路（文档 §12.2）。

engine 侧的时序与决策语义见 test_approval.py。
这里测的是「HTTP 决策能不能唤醒**挂起的** run」—— 整条链路的接缝。

★ 审批从「在线阻塞」改成「挂起」之后：run 落成 suspended（在等什么看 waiting_on；
  迁移 0030 起不再有 awaiting_approval）、决策后续跑到 succeeded、拒绝不终止、
  重复决策 409。没有进程在等，唤醒由端点触发。

★ 原先有一条 test_decision_arriving_first_is_not_lost 论证「用 BLPOP 而非
  pub/sub」。那个队列现在没有消费者，整条测试随机制一起删掉了。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
from atlas_agent.config import get_settings
from atlas_agent.db.session import fetch_one, get_sessionmaker
from atlas_agent.executor.compose import build_executor
from atlas_agent.main import create_app
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk

from tests.fake_uc import AS_USER
from tests.fakes import TurnModel, tool_call_chunk
from tests.test_runs_api import wait_for_status

#: 规划模式：模型提交计划（exit_plan_mode）→ 一次 kind=plan 的审批。它在任何 native 智能体上
#: 都存在，不依赖文件系统 / 沙箱，最适合测审批的接口链路（doc/native-approval-mode-design.html）
GUARDED_SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": ["write_todos"],
    "approval": {"default_mode": "plan"},
}
#: 批准计划必须带执行模式（且在允许范围里）
APPROVE = {"decision": "approved", "approval_mode": "auto"}


def make_app():
    app = create_app()
    app.state.executor = build_executor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: TurnModel(
            scripts=[
                [tool_call_chunk("exit_plan_mode", '{"plan":"先记待办"}', "c1")],
                [AIMessageChunk(content="办完了")],
            ]
        ),
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
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


async def _wait_suspended(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    """等到这一段挂起落库（审批登记在前，落成 suspended 在段收尾时）。"""
    run: dict[str, Any] = {}
    for _ in range(100):
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        if run["status"] == "suspended":
            return run
        await asyncio.sleep(0.1)
    raise AssertionError(f"run 没有挂起：{run}")


@pytest.mark.usefixtures("clean_db")
async def test_run_waits_suspended_on_the_approval(client: httpx.AsyncClient) -> None:
    """★ 执行器真的停在那里等 —— 而不是继续往下跑。等人是挂起，不是 awaiting_user。"""
    run_id, _ = await _start_guarded_run(client)

    run = await _wait_suspended(client, run_id)
    assert run["awaiting_user"] is False, run
    await asyncio.sleep(0.3)
    assert (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "suspended"


@pytest.mark.usefixtures("clean_db")
async def test_approve_resumes_the_run(client: httpx.AsyncClient) -> None:
    """★ 整条链路的接缝：HTTP 决策 → Redis → 阻塞中的执行器被唤醒。"""
    run_id, approval_id = await _start_guarded_run(client)

    res = await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json=APPROVE)
    assert res.status_code == 200, res.text

    run = await wait_for_status(client, run_id)
    assert run["status"] == "succeeded", run
    # ★ 计划获批 → 模式改成用户选的「自动」，并落到会话上（下一条消息就用它）
    thread = (await client.get(f"/v1/threads/{run['thread_id']}")).json()
    assert thread["approval_mode"] == "auto", thread
    assert thread["approval"]["mode"] == "auto"


@pytest.mark.usefixtures("clean_db")
async def test_approving_a_plan_requires_an_allowed_execution_mode(
    client: httpx.AsyncClient,
) -> None:
    """批准计划必须选执行模式，且在智能体允许的范围里 —— 模式是权限，不能由请求随便指定。"""
    run_id, approval_id = await _start_guarded_run(client)
    url = f"/v1/runs/{run_id}/approvals/{approval_id}"
    missing = await client.post(url, json={"decision": "approved"})
    assert missing.status_code == 400, missing.text
    assert (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"], "被错误地消费了"


@pytest.mark.usefixtures("clean_db")
async def test_the_allowed_modes_are_the_ones_registered_with_the_plan(
    client: httpx.AsyncClient,
) -> None:
    """可选的执行模式在登记时记下（刷新页面也拿得到），做决定时按记下的范围校验。"""
    from atlas_agent.db.models import Approval
    from sqlalchemy import update

    run_id, approval_id = await _start_guarded_run(client)
    (pending,) = (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"]
    assert pending["kind"] == "plan" and pending["execution_modes"] == ["manual", "auto"]

    # 记下的范围才算数：收窄成只许手动后，选自动被拒、选手动通过
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Approval)
            .where(Approval.approval_uuid == UUID(approval_id))
            .values(execution_modes=["manual"])
        )
        await session.commit()
    url = f"/v1/runs/{run_id}/approvals/{approval_id}"
    assert (await client.post(url, json=APPROVE)).status_code == 400
    ok = await client.post(url, json={"decision": "approved", "approval_mode": "manual"})
    assert ok.status_code == 200, ok.text
    await wait_for_status(client, run_id)


@pytest.mark.usefixtures("clean_db")
async def test_a_mode_outside_the_allowed_range_is_refused(client: httpx.AsyncClient) -> None:
    """发消息时选的模式必须在智能体允许的范围里；选过的模式会话会记住。"""
    spec = {**GUARDED_SPEC, "approval": {"default_mode": "auto", "allowed_modes": ["auto"]}}
    agent = await client.post(
        "/v1/agents", json={"slug": "auto-only", "name": "只许自动", "spec": spec}
    )
    thread = (await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})).json()
    bad = await client.post(
        f"/v1/threads/{thread['id']}/runs",
        json={"content": [{"type": "text", "text": "x"}], "approval_mode": "manual"},
    )
    assert bad.status_code == 400, bad.text
    detail = (await client.get(f"/v1/threads/{thread['id']}")).json()
    assert detail["approval"] == {"mode": "auto", "default_mode": "auto", "allowed_modes": ["auto"]}


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

    first = await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json=APPROVE)
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

    await _wait_suspended(client, run_id)

    # 模拟重启：旧执行器的 task 全部作废，换一个新的
    old = client._transport.app.state.executor  # type: ignore[attr-defined]
    await old.shutdown()
    fresh = build_executor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: TurnModel(
            scripts=[
                [tool_call_chunk("exit_plan_mode", '{"plan":"先记待办"}', "c1")],
                [AIMessageChunk(content="重启后办完了")],
            ]
        ),
    )
    client._transport.app.state.executor = fresh  # type: ignore[attr-defined]

    still = (await client.get(f"/v1/runs/{run_id}")).json()
    assert still["status"] == "suspended", f"被孤儿回收误杀了：{still}"

    # 决策到了 —— 新进程把它续起来
    res = await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json=APPROVE)
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
    await _wait_suspended(client, run_id)
    executor = client._transport.app.state.executor  # type: ignore[attr-defined]

    assert await executor.resume_if_ready(run_id) is False, "没决策就被续跑了"
    assert await executor.sweep_suspended() == 0

    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "suspended", run


@pytest.mark.usefixtures("clean_db")
async def test_an_old_approval_never_expires(client: httpx.AsyncClient) -> None:
    """★ 审批只由人决定：等多久都不替用户按拒绝处理（原先一天后由扫描判死）。"""
    from datetime import UTC, datetime, timedelta

    from atlas_agent.db.models import Approval
    from sqlalchemy import update

    run_id, approval_id = await _start_guarded_run(client)
    executor = client._transport.app.state.executor  # type: ignore[attr-defined]

    # 把它的创建时间推回到很久以前（模拟等了一个月）
    long_ago = datetime.now(UTC) - timedelta(days=30)
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Approval)
            .where(Approval.approval_uuid == UUID(approval_id))
            .values(created_at=long_ago)
        )
        await session.commit()

    assert await executor.sweep_suspended() == 0
    assert (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "suspended"
    async with get_sessionmaker()() as session:
        row = await fetch_one(session, Approval, Approval.approval_uuid == UUID(approval_id))
        assert row is not None and row.status == "pending", row.status if row else None

    # 人终于做了决定 —— 照样生效
    res = await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json=APPROVE)
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
    """★ CLI 在线等权限是 running + awaiting_user，扫描和续跑都不能碰它。

        原生   图已跳出、进程已结束 —— 真挂起（suspended），waiting_on 有值
        CLI    CLI 在 Pod 里发起的同步 RPC，**run 还在跑** —— bridge 正等着
               响应，只能在线等

    续跑它的话，第二条上游连接连上同一个 Pod，bridge 按 CLOSE_SUPERSEDED(4409)
    关掉第一个，整轮 runtime_crashed。真机验证时撞到过，症状是「审批批准了，
    子 run 却崩了」。迁移 0030 之前两者共用 awaiting_approval，只能靠有没有
    waiting_on 去猜。
    """
    from atlas_agent.db.models import Run
    from sqlalchemy import update

    run_id, _approval_id = await _start_guarded_run(client)
    await _wait_suspended(client, run_id)
    executor = client._transport.app.state.executor  # type: ignore[attr-defined]

    # 模拟 CLI 的在线等待：进程在跑（持有租约），等用户批准
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Run)
            .where(Run.run_uuid == UUID(run_id))
            .values(status="running", awaiting_user=True, waiting_on=None, owner="elsewhere")
        )
        await session.commit()

    assert await executor.sweep_suspended() == 0, "在线等待的 run 被当成挂起续跑了"
    assert await executor.resume_if_ready(UUID(run_id)) is False

    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["status"] == "running" and run["awaiting_user"] is True, run


@pytest.mark.usefixtures("clean_db")
async def test_cli_approvals_mark_the_run_awaiting_user(
    client: httpx.AsyncClient, redis: aioredis.Redis
) -> None:
    """★ CLI 的权限请求进同一张审批表；待决期间 run 标 awaiting_user，决定之后清掉。"""
    from atlas_agent.db.models import Approval
    from atlas_agent.host.runtime import _DbApprovals
    from sqlalchemy import update

    run_id, _approval_id = await _start_guarded_run(client)
    await _wait_suspended(client, run_id)
    # 原生那条审批先作废，只留 CLI 的这一条
    async with get_sessionmaker()() as session:
        await session.execute(update(Approval).values(status="expired"))
        await session.commit()

    approvals = _DbApprovals(get_sessionmaker(), redis, UUID(run_id))
    cli_approval = str(uuid4())
    assert await approvals.check(approval_id=cli_approval, tool_name="Bash", args={}) == "pending"
    assert (await client.get(f"/v1/runs/{run_id}")).json()["awaiting_user"] is True

    decided = await client.post(
        f"/v1/runs/{run_id}/approvals/{cli_approval}", json={"decision": "approved"}
    )
    assert decided.status_code == 200, decided.text
    assert await approvals.check(approval_id=cli_approval, tool_name="Bash", args={}) == "approved"
    assert (await client.get(f"/v1/runs/{run_id}")).json()["awaiting_user"] is False


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
    from atlas_agent.approval import RunApprovalGate
    from atlas_agent.db.models import Approval
    from atlas_agent.repositories.approval import _TOOL_NAME_MAX

    run_id, _ = await _start_guarded_run(client)

    long_name = "`" + " && ".join(f"cat /workspace/file-{i}.txt" for i in range(20)) + "`"
    assert len(long_name) > _TOOL_NAME_MAX, "这条测试的前提是名字真的超长"

    gate = RunApprovalGate(get_sessionmaker(), UUID(run_id))
    approval_id = str(uuid4())
    state = await gate.check(approval_id=approval_id, tool_name=long_name, args={"x": 1})

    assert state == "pending", f"长工具名没能登记，退化成了 {state!r}"

    async with get_sessionmaker()() as session:
        row = await fetch_one(session, Approval, Approval.approval_uuid == UUID(approval_id))
        assert row is not None, "approval 没落库 —— 事务回滚了"
        assert len(row.tool_name) <= _TOOL_NAME_MAX
        # 截断保留开头：排查时一眼能认出是哪条命令
        assert row.tool_name.startswith("`cat /workspace/file-0.txt")


@pytest.mark.usefixtures("clean_db")
async def test_cancelling_a_suspended_run_expires_its_approvals(client: httpx.AsyncClient) -> None:
    """★ run 进入终态时还挂着的审批一律作废（c4-model/agent/code-approval.html）：
    取消之后刷新页面，待决端点不再列出它，弹窗不会回来；事后再点也是冲突，不会续跑。"""
    from atlas_agent.db.models import Approval

    run_id, approval_id = await _start_guarded_run(client)
    await _wait_suspended(client, run_id)

    assert (await client.post(f"/v1/runs/{run_id}/cancel")).status_code == 200
    assert (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "cancelled"
    assert (await client.get(f"/v1/runs/{run_id}/approvals")).json()["data"] == []
    async with get_sessionmaker()() as session:
        row = await fetch_one(session, Approval, Approval.approval_uuid == UUID(approval_id))
        assert row is not None and row.status == "expired", row.status if row else None

    late = await client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", json=APPROVE)
    assert late.status_code == 409, late.text
