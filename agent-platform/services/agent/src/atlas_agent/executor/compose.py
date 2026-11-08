"""Run 执行器的组合根：把派发、一段的执行、续跑和运行时装配在一起。

依赖方向：派发 → 一段的执行、派发 → 续跑 → 派发的仓储；执行只在接管时用续跑
（c4-model/agent/executor/code-executor.html）。运行时（原生、CLI）在这里构造并注册，执行器
不自己装配运行时；子智能体起子 run 只拿到派发这一个回调，不认识执行器。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_jobs import JobScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..instance import instance_id
from .assembly import CapabilityAssembly
from .continuation import Continuation
from .run_models import ModelBuilder, default_model_builder
from .runtime import NativeRuntime
from .scheduling import RunWorker
from .segment import RuntimeRegistry, SegmentExecution

if TYPE_CHECKING:
    from ..memory import MemoryRuntime


def build_executor(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    model_builder: ModelBuilder | None = None,
    cli_runtime: Any = None,
    memory: MemoryRuntime | None = None,
    instance: str | None = None,
) -> RunWorker:
    """装配一个实例上的 Run 执行器。兜底和续租要另外注册到定时任务库（register_jobs）。

    model_builder  可注入：单测塞一个假模型，整条执行链路即可脱离网络运行
    cli_runtime    None = 本部署没接会话 Pod：kind="cli" 的智能体明确报错，不悄悄按 native 跑
    instance       租约的 owner；默认取本进程的实例号（测试里可以在一个进程里扮演几个实例）
    """
    build_model = model_builder or default_model_builder
    owner = instance or instance_id()
    worker: RunWorker | None = None

    async def dispatch(run_id: UUID) -> None:
        # 子 run 在父 run 所在的实例上起：与用户发起的 run 完全同一条链路
        assert worker is not None, "执行器还没装配完"
        await worker.dispatch(run_id)

    # assembly 是进程级复用的：它持有实例态（沙箱 / 编码后端），每轮新建会把那些状态打散
    assembly = CapabilityAssembly(
        sessionmaker,
        settings,
        build_model,
        dispatch_run=dispatch,
        # 常驻记忆与记忆工具（doc/memory-design.html §4–§5）
        memory=memory,
    )
    runtimes = RuntimeRegistry(native=NativeRuntime(assembly), cli=cli_runtime)
    continuation = Continuation(sessionmaker, settings, instance=owner)
    segment = SegmentExecution(
        sessionmaker, settings, instance=owner, runtimes=runtimes, continuation=continuation
    )
    worker = RunWorker(
        sessionmaker,
        settings,
        instance=owner,
        segment=segment,
        continuation=continuation,
        model_builder=build_model,
    )
    return worker


def register_jobs(jobs: JobScheduler, worker: RunWorker, settings: Settings) -> None:
    """把执行器的两个实例任务注册到定时任务库：兜底每 10 秒左右一轮（各实例错开），续租每 lease/3。

    ★ 宿主停机时先 worker.shutdown()（交接）再 jobs.stop()：交接期间在跑的 run 还要续租。
    ★ api 角色不跑兜底：它不执行 run，抢到了也跑不了。
    """
    if worker.executes:
        every = settings.run_recovery_interval_s
        jobs.register(worker.recovery, scope="instance", every=every, jitter=every * 0.3)
    jobs.register(worker.renewer, scope="instance", every=max(settings.run_lease_s / 3, 0.2))
