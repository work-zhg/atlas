"""定时任务的只读管理接口（c4-model/infra-platform/code-jobs.html · JobRoutes）。

    GET   /api/v1/jobs      本实例的实例任务（刷新工具定义）：周期、正在不在跑、下一轮、
                            运行与失败次数

★ 要 mcp:manage（和其他管理接口同一个权限）；管理页在 Atlas 前端的「定时任务」里。
★ 路由由 atlas_jobs 给出，这里只接上本服务的鉴权。
"""

from __future__ import annotations

from atlas_jobs import JobRoutes, JobScheduler
from fastapi import APIRouter, Depends, Request

from ..auth import ManagerDep

__all__ = ["jobs_router"]


async def _job_admin(request: Request, operator: ManagerDep) -> None:
    request.state.job_admin = operator


class _AdminGuard:
    """鉴权在路由依赖里做完了，这里只给出操作人。"""

    async def view(self, request: Request) -> str:
        return str(request.state.job_admin.uid)


def jobs_router(jobs: JobScheduler) -> APIRouter:
    router = APIRouter()
    router.include_router(
        JobRoutes(jobs, _AdminGuard(), prefix="/api/v1/jobs").router,
        dependencies=[Depends(_job_admin)],
    )
    return router
