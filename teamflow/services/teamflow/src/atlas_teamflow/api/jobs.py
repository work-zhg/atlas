"""定时任务的只读管理接口（c4-model/infra-platform/code-jobs.html · JobRoutes）。

    GET   /api/v1/jobs      本实例的实例任务（协作者对账、扫描待处理节点）：周期、正在不在跑、
                            下一轮、运行与失败次数

★ 要 settings:manage（系统设置，平台管理员）；页面在「系统设置 › 定时任务」。
★ 独立 Worker 进程里的扫描不经这里（Worker 没有 HTTP 接口）：这里只是回应请求的 API 实例的情况。
★ 路由由 atlas_jobs 给出，这里只接上 TeamFlow 的鉴权。
"""

from __future__ import annotations

from atlas_jobs import JobRoutes, JobScheduler
from fastapi import APIRouter, Depends, Request

from .deps import PrincipalDep

__all__ = ["jobs_router"]


async def _job_admin(request: Request, principal: PrincipalDep) -> None:
    principal.require("settings:manage")
    request.state.job_admin = principal


class _AdminGuard:
    """鉴权在路由依赖里做完了，这里只给出操作人。"""

    async def view(self, request: Request) -> str:
        return str(request.state.job_admin.uuid)


def jobs_router(jobs: JobScheduler) -> APIRouter:
    router = APIRouter()
    router.include_router(
        JobRoutes(jobs, _AdminGuard(), prefix="/api/v1/jobs").router,
        dependencies=[Depends(_job_admin)],
    )
    return router
