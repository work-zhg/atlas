"""定时任务（c4-model/infra-platform/code-jobs.html）：嵌入宿主应用，应用只写 execute，
调度、线程、停机交给库。

    class Recovery(Job):
        type = "agent.run_recovery"

        async def execute(self, ctx: JobContext, params: NoParams) -> None: ...

    jobs = JobScheduler()
    jobs.register(Recovery(worker), scope="instance", every=10, jitter=3)
    jobs.start()            # 宿主 lifespan 里
    ...
    await jobs.stop()       # 宿主自己的收尾做完之后

这一版只有实例任务（和 Spring 的 @Scheduled 一样）：定义只在代码里，每个进程各跑一份，
不抢行、不要租约、不写执行记录，只出指标。集群任务（库里定义、全集群一份，和 Quartz 一样）
还没实现。
"""

from .instance import InstanceMetrics
from .job import Job, JobContext, NoParams
from .scheduler import JobScheduler

__all__ = ["InstanceMetrics", "Job", "JobContext", "JobScheduler", "NoParams"]
