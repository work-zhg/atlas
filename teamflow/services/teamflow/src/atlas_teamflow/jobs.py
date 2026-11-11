"""定时任务（定时任务库 atlas_jobs 的实例任务，c4-model/infra-platform/code-jobs.html）。

    teamflow.collaborator_reconcile   仓库协作者对账：以团队成员身份为准修正（成员按部门展开的变化、
                                      在 Gitee 上被手工改动的）
    teamflow.node_sweep               扫描待处理节点：接手崩溃实例留下的节点、补漏掉的唤醒
                                      （AgentSupervisor.resume）

多实例时每个实例都跑，操作幂等（节点由租约锁保证只有一个实例处理）。循环、出错隔离、停机交给库；
运行情况在 /api/v1/jobs 看。
"""

from __future__ import annotations

import logging
from typing import Any

from atlas_jobs import Job, JobContext, NoParams
from sqlalchemy import select

from .agents.supervisor import AgentSupervisor
from .db.models import Project
from .db.session import get_sessionmaker
from .errors import TFError
from .process.repo_host import RepoHost
from .process.repository import sync_project_collaborators

__all__ = ["CollaboratorReconcile", "NodeSweep"]

log = logging.getLogger("atlas_teamflow")


class CollaboratorReconcile(Job):
    type = "teamflow.collaborator_reconcile"
    description = "仓库协作者对账"

    def __init__(self, uc: Any, host: RepoHost) -> None:
        self._uc, self._host = uc, host

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        async with get_sessionmaker()() as s:
            for pr in await s.scalars(
                select(Project).where(Project.status == "active", Project.repo_status == "ready")
            ):
                try:
                    await sync_project_collaborators(s, self._uc, self._host, pr)
                except TFError as exc:
                    # 一个项目失败不影响别的项目
                    log.warning("对账项目 %s 的仓库协作者失败：%s", pr.uuid, exc.message)
            await s.commit()


class NodeSweep(Job):
    type = "teamflow.node_sweep"
    description = "扫描待处理节点"

    def __init__(self, supervisor: AgentSupervisor) -> None:
        self._supervisor = supervisor

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await self._supervisor.resume()
