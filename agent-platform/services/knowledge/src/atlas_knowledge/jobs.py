"""定时任务（定时任务库 atlas_jobs 的实例任务，c4-model/infra-platform/code-jobs.html）。

    knowledge.ingest_sweep          入库巡检：把「停在入库中间状态、却没有任务持锁」的文档
                                    重新投递（worker 被杀后的恢复，document/runner.py
                                    recover_stalled）；building 却没人持锁的索引版本也重新投递
                                    （versions.recover_rebuilds）
    knowledge.enhance_cache_cleanup 删掉超过保留期（30 天）的入库增强缓存
                                    （segment/enhance_cache.py）

每个 API 副本各跑一份：重复投递、重复删都是无害的。循环、出错隔离、停机交给库；
运行情况在运营接口 /ops/v1/jobs 看。
"""

from __future__ import annotations

from atlas_jobs import Job, JobContext, NoParams

from .db.session import get_sessionmaker
from .document.runner import recover_stalled
from .index.versions import recover_rebuilds
from .runtime import Runtime
from .segment.enhance_cache import cleanup

__all__ = ["EnhanceCacheCleanup", "IngestSweep"]


class IngestSweep(Job):
    type = "knowledge.ingest_sweep"
    description = "入库巡检（含索引版本重建）"

    def __init__(self, runtime: Runtime) -> None:
        self._rt = runtime

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await recover_stalled(self._rt, get_sessionmaker())
        await recover_rebuilds(self._rt, get_sessionmaker())


class EnhanceCacheCleanup(Job):
    type = "knowledge.enhance_cache_cleanup"
    description = "删掉超过保留期的入库增强缓存"

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await cleanup(get_sessionmaker())
