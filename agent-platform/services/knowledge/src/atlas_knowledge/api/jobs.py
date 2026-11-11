"""定时任务的只读管理接口（c4-model/infra-platform/code-jobs.html · JobRoutes）。

    GET   /v1/jobs      本实例的实例任务（入库巡检）：周期、正在不在跑、下一轮、运行与失败次数

★ 要 job:manage（Atlas 应用的操作码，atlas-server auth/catalog.py 登记）：和 Atlas 服务自己的
  定时任务同一个权限，管理页在 Atlas 前端的「定时任务」里。
★ 路由由 atlas_jobs 给出，这里只接上本服务的鉴权。
"""

from __future__ import annotations

from typing import Annotated

from atlas_jobs import JobRoutes, JobScheduler
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import Principal, current_principal

__all__ = ["jobs_router"]

JOB_MANAGE = "job:manage"


async def _job_admin(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> None:
    if not principal.has(JOB_MANAGE):
        raise HTTPException(status_code=403, detail=f"没有查看定时任务的权限（{JOB_MANAGE}）")
    request.state.job_admin = principal


class _AdminGuard:
    """鉴权在路由依赖里做完了，这里只给出操作人。"""

    async def view(self, request: Request) -> str:
        return str(request.state.job_admin.uid)


def jobs_router(jobs: JobScheduler) -> APIRouter:
    router = APIRouter()
    router.include_router(
        JobRoutes(jobs, _AdminGuard(), prefix="/v1/jobs").router, dependencies=[Depends(_job_admin)]
    )
    return router
