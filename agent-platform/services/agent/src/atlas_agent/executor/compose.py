"""Run 执行器的组合根：把调度、一段的执行、续跑和运行时装配在一起。

依赖方向：调度 → 一段的执行 → 续跑 → 调度的队列；续跑不依赖执行（c4-model/agent/
code-executor.html）。运行时（原生、CLI）在这里构造并注册，执行器不自己装配运行时；
子智能体起子 run 只拿到入队这一个回调，不认识执行器。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

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
    """装配一个实例上的 Run 执行器。

    model_builder  可注入：单测塞一个假模型，整条执行链路即可脱离网络运行
    cli_runtime    None = 本部署没接会话 Pod：kind="cli" 的智能体明确报错，不悄悄按 native 跑
    instance       租约的 owner；默认取本进程的实例号（测试里可以在一个进程里扮演几个实例）
    """
    build_model = model_builder or default_model_builder
    owner = instance or instance_id()
    worker: RunWorker | None = None

    async def submit(run_id: UUID) -> None:
        # 委派与续跑放回队列之后都走这里：与用户发起的 run 完全同一条链路
        assert worker is not None, "执行器还没装配完"
        await worker.submit(run_id)

    # assembly 是进程级复用的：它持有实例态（沙箱 / 编码后端），每轮新建会把那些状态打散
    assembly = CapabilityAssembly(
        sessionmaker,
        settings,
        build_model,
        submit_run=submit,
        # 常驻记忆与记忆工具（doc/memory-design.html §4–§5）
        memory=memory,
    )
    runtimes = RuntimeRegistry(native=NativeRuntime(assembly), cli=cli_runtime)
    continuation = Continuation(sessionmaker, settings, instance=owner, requeue=submit)
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
