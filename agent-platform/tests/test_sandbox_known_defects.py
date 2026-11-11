"""沙箱命令执行的缺陷回归（bench/doc/sandbox-eng-cases.html §3，修法见
doc/design/sandbox-defect-fixes.html）。原先写成 xfail(strict) 的失败用例，修好后转为常规用例。

    D3  提交前先记「提交中」：进程死在「请求已到 execd、执行账本还没补上 id」时，接管重放不再提交
    D5  OpenSandbox 不可达的异常翻成「暂时连不上」：跟随中要重新取地址时遇上它，按断线重试
    D4  活动账本丢了数据（Redis 清空），跟随中的命令会被重新登记，闲置回收不会把它当成闲置

    D1  停机交接等不到安全点、中止任务时，不中断沙箱里正在跑的命令
    D2  租约丢失后停本地执行（取消任务），同样不中断接管者正在跟随的命令
    D9  用户取消送得到正在执行的工具：命令在跑时取消，1～2 秒内中断命令、run 进入 cancelled

D1 / D2 / D9 在执行器层测：真实执行器 + 假 OpenSandbox（命令真跑在 bash 里）+ moto 的内存桶，
命令要人工批准（manual 模式），批准后在假沙箱里跑起来，再触发交接 / 租约丢失 / 取消，
看命令还活着没有。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import boto3
import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import Approval, Run
from atlas_agent.db.session import fetch_one, get_sessionmaker
from atlas_agent.executor.scheduling import RunWorker
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox import exec as exec_module
from atlas_agent.sandbox.base.execution.execd import ExecdChannel
from atlas_agent.sandbox.base.execution.values import CommandSpec
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.exec import ExecutionLedger, PollingPolicy, ShellSandbox
from atlas_agent.sandbox.native import WorkspaceRef
from atlas_engine.contracts import SandboxUnavailable
from atlas_engine.contracts.spec import SandboxSpec
from atlas_jobs import NoParams
from moto import mock_aws
from sqlalchemy import update

from tests.fakes import tool_call_chunk
from tests.opensandbox_fake import LIFECYCLE_URL, FakeOpenSandbox, fake_settings
from tests.sandbox_env import component, new_workspace, workspace_dir
from tests.test_horizontal_scaling import (
    HangingModel,
    _client,
    _ctx,
    _executor,
    executors,  # noqa: F401  # 夹具：两个执行器实例
)


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
def ws() -> WorkspaceRef:
    return new_workspace()


@pytest.fixture
async def redis(clean_db: None) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await client.aclose()


class _ProcessDied(BaseException):
    """进程在这一刻没了：之后的代码一行都不会执行（不是取消 —— 取消还会走中断与收尾）。"""


def _sandbox(c: SandboxComponent, ws: WorkspaceRef) -> ShellSandbox:
    """同一个 run_id、同一个 Redis：接管的实例重放同一个调用时看到的就是这本账。"""
    return ShellSandbox(
        native=c.native,
        exec=c.exec,
        ledger=c.ledger,
        ws=ws,
        run_id=ws.root_thread_uuid,
        spec=SandboxSpec(),
        polling=PollingPolicy(0.02, 0.05),
    )


async def _until(cond: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "等待超时"
        await asyncio.sleep(0.02)


async def _replay_settles(sbx: ShellSandbox, cmd: str, call: str) -> None:
    """接管者重放同一个调用：接上原命令、或如实报「结果未知」都可以 —— 只要不再跑一遍。"""
    with contextlib.suppress(SandboxUnavailable):
        await sbx.aexecute(cmd, exec_id=call)
    await asyncio.sleep(0.6)  # 万一重新提交了，等它跑完再数


# ──────────────────────────────────────────────── D3


async def test_d3_death_between_init_and_ledger_does_not_rerun(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = "echo x >> marker; sleep 0.3; echo done"
    marker = workspace_dir(fake, ws) / "marker"
    put = ExecutionLedger.put

    async def dies(self: ExecutionLedger, *a: Any, **kw: Any) -> None:
        raise _ProcessDied

    monkeypatch.setattr(ExecutionLedger, "put", dies)
    with pytest.raises(_ProcessDied):
        await _sandbox(component(fake, redis), ws).aexecute(cmd, exec_id="c1")
    monkeypatch.setattr(ExecutionLedger, "put", put)

    await _replay_settles(_sandbox(component(fake, redis), ws), cmd, "c1")
    assert marker.read_text() == "x\n", "接管重放把命令又跑了一遍"


async def test_d3_death_during_submit_does_not_rerun(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = "echo x >> marker; sleep 0.3; echo done"
    marker = workspace_dir(fake, ws) / "marker"
    c = component(fake, redis)
    await _sandbox(c, ws).aexecute("true")  # 先把沙箱建好：死要落在提交上
    start = ExecdChannel.start
    background: list[asyncio.Future[str]] = []

    async def dies(self: ExecdChannel, sandbox_uuid: Any, spec: CommandSpec) -> str:
        # 请求发出去了（execd 开跑），进程在 init 回来之前没了：没有中断、没有收尾
        background.append(asyncio.ensure_future(start(self, sandbox_uuid, spec)))
        await _until(lambda: len(fake.created[0].commands) == 2)
        raise _ProcessDied

    fake.submit_delay_s = 0.2
    monkeypatch.setattr(ExecdChannel, "start", dies)
    with pytest.raises(_ProcessDied):
        await _sandbox(c, ws).aexecute(cmd, exec_id="c1")
    monkeypatch.setattr(ExecdChannel, "start", start)
    fake.submit_delay_s = 0.0

    await _replay_settles(_sandbox(component(fake, redis), ws), cmd, "c1")
    for f in background:
        with contextlib.suppress(Exception):
            await f
    assert marker.read_text() == "x\n", "接管重放把命令又跑了一遍"


# ──────────────────────────────────────────────── D5


class _FlakyLifecycle(httpx.AsyncBaseTransport):
    """lifecycle server 一时不可达（execd 照常）：down 期间对它的请求一律连接失败。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.down = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.down and str(request.url).startswith(LIFECYCLE_URL):
            raise httpx.ConnectError("lifecycle server 不可达", request=request)
        return await self.inner.handle_async_request(request)


async def test_d5_lifecycle_outage_while_following_is_retried_like_a_disconnect(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    flaky = _FlakyLifecycle(fake.async_transport)
    c = SandboxComponent.build(
        fake_settings(), redis, transport=flaky, files_transport=fake.sync_transport
    )
    sbx = _sandbox(c, ws)
    task = asyncio.create_task(sbx.aexecute("for i in 1 2 3 4 5 6; do echo $i; sleep 0.4; done"))
    await _until(lambda: fake.created and fake.created[0].commands)
    await asyncio.sleep(0.3)
    flaky.down = True  # lifecycle 先挂
    fake.break_execd(1)  # 再断一次线：地址缓存作废，下一次要经 lifecycle 重新取
    await asyncio.sleep(1.5)  # 第一次重连（约 1 秒后）正好撞上 lifecycle 不可达
    flaky.down = False
    r = await task
    assert r.output == "".join(f"{i}\n" for i in range(1, 7)) and r.exit_code == 0


# ──────────────────────────────────────────────── D4


async def test_d4_followed_command_is_registered_again_after_redis_loses_it(
    fake: FakeOpenSandbox, redis: Any, ws: WorkspaceRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exec_module, "_CONFIRM_RUNNING_S", 0.1)
    sbx = _sandbox(component(fake, redis), ws)
    task = asyncio.create_task(sbx.aexecute("sleep 1.5; echo done"))
    await _until(lambda: fake.created and fake.created[0].commands)
    ref = await sbx._native.current(ws)
    assert ref is not None
    keys = [k async for k in redis.scan_iter(f"atlas:sandbox:{ref.sandbox_uuid}:*")]
    await redis.delete(*keys)  # Redis 丢了这个沙箱的活动账本
    await asyncio.sleep(0.4)
    activity = await sbx._exec.activity(ref.sandbox_uuid)
    assert activity.busy(), "跟随中的命令没有重新登记：闲置回收会把它连同沙箱删掉"
    assert (await task).output == "done\n"


# ──────────────────────────────────────────────── D1 / D2 / D9（执行器层）

_SPEC = {
    "system_prompt": "p",
    "model": {"model": "claude-opus-5"},
    "tool_names": ["filesystem", "bash"],
    "approval": {"default_mode": "manual", "allowed_modes": ["manual"]},
}
#: 一条 30 秒的命令：交接 / 租约丢失发生时它还在跑
_LONG = '{"command": "echo x >> marker; sleep 30; echo done"}'
_BUCKET = "atlas-test"


def _command_model(*_a: Any, **_k: Any) -> HangingModel:
    """第一次调用发起那条命令，之后的调用挂住（命令结束前不该再走到模型）。"""
    return HangingModel(hang_from=2, scripts=[[tool_call_chunk("execute", _LONG, "c1")], []])


@pytest.fixture
def sandboxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOpenSandbox]:
    """执行器装配的沙箱组件接到假 OpenSandbox，工作区接到 moto 的内存桶。"""
    fake = FakeOpenSandbox(tmp_path)
    real = SandboxComponent.build

    def build(cls: type, settings: Any, redis: Any, **kw: Any) -> SandboxComponent:
        return real(
            fake_settings(),
            redis,
            transport=fake.async_transport,
            files_transport=fake.sync_transport,
        )

    monkeypatch.setattr(SandboxComponent, "build", classmethod(build))
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=_BUCKET)
        yield fake
    fake.close()


def _sandboxed_executor(made: list[RunWorker], name: str, **overrides: Any) -> RunWorker:
    return _executor(
        made, name, _command_model, sandbox_enabled=True, oss_bucket=_BUCKET, **overrides
    )


async def _pending_approval(run_id: UUID) -> UUID | None:
    async with get_sessionmaker()() as s:
        row = await fetch_one(
            s, Approval, Approval.run_uuid == run_id, Approval.status == "pending"
        )
        return row.approval_uuid if row is not None else None


async def _start_long_command(executor: RunWorker, fake: FakeOpenSandbox) -> tuple[UUID, Any]:
    """建会话、发一轮：模型发起那条命令 → 人工批准 → 等它在假沙箱里跑起来。→ (run_id, 命令)"""
    async with _client(executor) as client:
        agent = await client.post(
            "/v1/agents", json={"slug": f"d1-{uuid4().hex[:6]}", "name": "沙箱", "spec": _SPEC}
        )
        assert agent.status_code == 201, agent.text
        thread = await client.post(
            "/v1/threads", json={"agent_id": agent.json()["id"], "title": "t"}
        )
        res = await client.post(
            f"/v1/threads/{thread.json()['id']}/runs",
            json={"content": [{"type": "text", "text": "跑一条命令"}]},
        )
        assert res.status_code == 202, res.text
        run_id = UUID(res.json()["run_id"])
        deadline = asyncio.get_running_loop().time() + 20
        while (approval := await _pending_approval(run_id)) is None:
            assert asyncio.get_running_loop().time() < deadline, "等不到审批"
            await asyncio.sleep(0.05)
        decided = await client.post(
            f"/v1/runs/{run_id}/approvals/{approval}", json={"decision": "approved"}
        )
        assert decided.status_code == 200, decided.text
    await _until(lambda: bool(fake.created and fake.created[0].commands), timeout=20)
    (cmd,) = fake.created[0].commands.values()
    return run_id, cmd


@pytest.mark.usefixtures("clean_db")
async def test_d1_handoff_does_not_interrupt_the_running_command(
    executors: list[RunWorker],  # noqa: F811
    sandboxed: FakeOpenSandbox,
) -> None:
    a = _sandboxed_executor(executors, "instance-a", drain_timeout_s=0.3)
    _, cmd = await _start_long_command(a, sandboxed)
    await a.drain()  # 命令要跑 30 秒，等不到安全点：0.3 秒后中止任务、交接
    await asyncio.sleep(0.5)
    assert cmd.proc.poll() is None, "交接把沙箱里正在跑的命令杀掉了（接手的实例只能读到被杀的结果）"


@pytest.mark.usefixtures("clean_db")
async def test_d2_lost_lease_does_not_interrupt_the_running_command(
    executors: list[RunWorker],  # noqa: F811
    sandboxed: FakeOpenSandbox,
) -> None:
    a = _sandboxed_executor(executors, "instance-a")
    run_id, cmd = await _start_long_command(a, sandboxed)
    slot = a._slots[run_id]
    # 假死期间租约过期、被别的实例接走（它按执行账本接上了这条命令）；本实例复活后续租时发现
    async with get_sessionmaker()() as s:
        await s.execute(update(Run).where(Run.run_uuid == run_id).values(owner="instance-b"))
        await s.commit()
    await a.renewer.execute(_ctx(), NoParams())
    assert slot.task is not None
    await asyncio.wait([slot.task], timeout=5)
    assert slot.lost, "续租没有发现租约已丢（场景没生效）"
    await asyncio.sleep(0.5)
    assert cmd.proc.poll() is None, "租约丢失后把接管者正在跟随的命令杀掉了"


@pytest.mark.usefixtures("clean_db")
async def test_d9_cancel_reaches_the_running_command(
    executors: list[RunWorker],  # noqa: F811
    sandboxed: FakeOpenSandbox,
) -> None:
    """命令在跑时取消：不等它跑完（30 秒），1～2 秒内中断命令、run 进入 cancelled。"""
    a = _sandboxed_executor(executors, "instance-a")
    run_id, cmd = await _start_long_command(a, sandboxed)
    await asyncio.sleep(0.5)  # 命令已经跑起来、图卡在工具里（没有新帧）
    async with _client(a) as client:
        res = await client.post(f"/v1/runs/{run_id}/cancel")
        assert res.status_code == 200, res.text
    started = asyncio.get_running_loop().time()
    await _until(lambda: cmd.proc.poll() is not None, timeout=5)
    deadline = started + 5
    while (await _run_status(run_id)) != "cancelled":
        assert asyncio.get_running_loop().time() < deadline, "取消后 run 迟迟没有进入 cancelled"
        await asyncio.sleep(0.05)
    assert asyncio.get_running_loop().time() - started < 3


async def _run_status(run_id: UUID) -> str | None:
    async with get_sessionmaker()() as s:
        row = await fetch_one(s, Run, Run.run_uuid == run_id)
        return row.status if row is not None else None
