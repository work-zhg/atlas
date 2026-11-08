"""RunExecutor 协议：Run 执行器对组件外的唯一入口（c4-model/agent/executor/code-executor.html）。

调用方只管「让这个 run 跑起来」：本实例接得下就当场跑，接不下也不报错，兜底任务会处理
（design-run-dispatch.html）。由哪个实例跑、租约、续跑都在执行器里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

#: 派发一个 run：子智能体服务起子 run 只依赖它，不认识执行器（RunExecutor.dispatch）
DispatchRun = Callable[[UUID], Awaitable[None]]


class RunExecutor(Protocol):
    async def dispatch(self, run_id: UUID) -> None:
        """派发：run 行已建好（queued），在本实例抢锁开跑。不等这一轮跑完 —— HTTP 请求要
        立刻返回 run_id。"""
        ...

    async def cancel(self, run_id: UUID) -> None: ...

    async def resume_if_ready(self, run_id: UUID) -> bool:
        """等的事都有结果了就把这个挂起的 run 放回排队，并在本实例抢锁。返回是否放回去了。

        ★ 为什么要进协议：续跑的入口有一个在执行器之外 —— 审批被决定时，HTTP 端点
          （api/v1/runs.py::decide_approval）经 decided 调它，run 就在接到决定的实例上续。
        """
        ...

    async def decided(self, run_id: UUID, approval_id: UUID) -> None:
        """审批组件的 DecisionListener：用户做了决定，等的事项都有结果了就续跑。"""
        ...

    async def shutdown(self) -> None:
        """停机交接：关准入、在安全点停下、清租约，剩下的交给别的实例的兜底任务。"""
        ...
