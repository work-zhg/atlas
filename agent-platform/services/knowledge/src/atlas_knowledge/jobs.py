"""定时任务（定时任务库 atlas_jobs 的实例任务，c4-model/infra-platform/code-jobs.html）。

    knowledge.ingest_sweep   入库巡检：把「停在入库中间状态、却没有任务持锁」的文档重新投递
                             （worker 被杀后的恢复，indexing/runner.py recover_stalled）

每个 API 副本各跑一份：重复投递是无害的。循环、出错隔离、停机交给库；运行情况在 /v1/jobs 看。
"""

from __future__ import annotations

from atlas_jobs import Job, JobContext, NoParams

from .db.session import get_sessionmaker
from .indexing.runner import recover_stalled
from .runtime import Runtime

__all__ = ["IngestSweep"]


class IngestSweep(Job):
    type = "knowledge.ingest_sweep"
    description = "入库巡检"

    def __init__(self, runtime: Runtime) -> None:
        self._rt = runtime

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await recover_stalled(self._rt, get_sessionmaker())
