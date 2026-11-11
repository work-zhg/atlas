"""定时任务的管理接口（c4-model/infra-platform/code-jobs.html · JobRoutes）。

    GET   /v1/jobs      本实例的实例任务（兜底、续租）：周期、正在不在跑、下一轮、运行与失败次数

★ 要 job:manage：兜底、续租是执行器的系统机制，只给平台管理员看。这一版只读。
★ 实例任务每个进程各跑一份，返回的是回应请求的那个实例的情况；实例标识与 run 租约的 owner 一致。
★ 路由由 atlas_jobs 给出，这里只接上 Atlas 的鉴权：依赖先按操作码判、把主体放进 request.state，
  JobGuard 从那里取操作人。
"""

from __future__ import annotations

from typing import Annotated

from atlas_jobs import JobRoutes, JobScheduler
from fastapi import APIRouter, Depends, Request

from ...auth.authz import require
from ...auth.dependency import current_principal
from ...auth.principal import Principal
from ...instance import instance_id

__all__ = ["jobs_router"]


async def _job_admin(
    request: Request, principal: Annotated[Principal, Depends(require("job:manage"))]
) -> None:
    request.state.job_admin = principal


class _AdminGuard:
    """鉴权在路由依赖里做完了，这里只给出操作人。"""

    async def view(self, request: Request) -> str:
        return str(request.state.job_admin.uid)


def jobs_router(jobs: JobScheduler) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(current_principal)])
    router.include_router(
        JobRoutes(jobs, _AdminGuard(), instance=instance_id()).router,
        dependencies=[Depends(_job_admin)],
    )
    return router
