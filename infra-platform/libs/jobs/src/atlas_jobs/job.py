"""任务接口：宿主只写 execute（c4-model/infra-platform/code-jobs.html 的 Job、JobContext）。"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict


class NoParams(BaseModel):
    """没有参数的任务。实例任务一般如此：周期写在代码里，没有可调的东西。"""

    model_config = ConfigDict(extra="forbid")


class Job:
    """宿主实现的任务。

    type         全局唯一的类型名，带应用前缀（agent.run_recovery）
    description  给人看的名字（管理页显示）；不写就显示 type
    Params       参数的 pydantic 模型；不声明就是没有参数
    execute      写成 async def 在宿主的事件循环上跑（里面不能有阻塞调用）；
                 写成 def 在库自己的线程池里跑
    """

    type: ClassVar[str]
    description: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel]] = NoParams

    def execute(self, ctx: JobContext, params: Any) -> Awaitable[None] | None:
        raise NotImplementedError


@dataclass(frozen=True)
class JobContext:
    """交给任务的这一轮。"""

    job_name: str
    #: 这一轮的触发时刻
    fired_at: datetime

    def still_held(self) -> bool:
        """这一轮还归不归本进程。实例任务不抢行、没有租约，始终是 True。"""
        return True
