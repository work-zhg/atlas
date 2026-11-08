"""代码里注册的任务（c4-model/infra-platform/code-jobs.html 的 JobRegistry）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .job import Job


@dataclass(frozen=True)
class InstanceJob:
    """一个实例任务：任务本身和它的周期。周期只在代码里，不进库。"""

    job: Job
    #: 两轮之间隔多久（秒）
    every: float
    #: 每轮在 every 上随机加减的秒数，几个进程的同一个任务因此错开
    jitter: float


class JobRegistry:
    """类型名 → 任务。

    ★ 这一版只有实例任务：每个进程各跑一份、不抢行、不写执行记录。集群任务（库里定义、
      全集群一份）还没实现。
    ★ 注册的是任务对象而不是类：实例任务多半要拿着宿主的对象（比如 Run 执行器），
      由宿主构造好交进来。
    """

    def __init__(self) -> None:
        self._instance: dict[str, InstanceJob] = {}

    def register(
        self,
        job: Job,
        *,
        scope: Literal["instance"],
        every: float,
        jitter: float = 0.0,
    ) -> None:
        if scope != "instance":
            raise ValueError(f"不支持的作用域：{scope!r}（这一版只有实例任务）")
        name = getattr(type(job), "type", "")
        if not name:
            raise ValueError(f"{type(job).__name__} 没有声明 type")
        if name in self._instance:
            raise ValueError(f"任务类型重复注册：{name}")
        if every <= 0:
            raise ValueError(f"{name} 的间隔必须大于 0：{every}")
        if not 0 <= jitter < every:
            raise ValueError(f"{name} 的抖动必须在 [0, 间隔) 之内：{jitter}")
        self._instance[name] = InstanceJob(job=job, every=every, jitter=jitter)

    def instance_jobs(self) -> list[InstanceJob]:
        return list(self._instance.values())
