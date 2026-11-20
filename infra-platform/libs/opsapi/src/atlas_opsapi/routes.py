"""运营接口的路由，统一挂在 /ops/v1（网关不转发这个前缀）。

    POST /ops/v1/tenants/{tenant_uuid}/suspend    挂起（幂等）
    POST /ops/v1/tenants/{tenant_uuid}/resume     恢复（幂等；已经在删的拒绝）
    POST /ops/v1/tenants/{tenant_uuid}/purge      删一批：{deleted, remaining}；
                                                  remaining 为真就再调一次
    GET  /ops/v1/tenant-states                    本系统里各租户的状态
    /ops/v1/configs                               系统参数（应用配置库的管理接口，scope=SYSTEM）
    /ops/v1/jobs                                  系统任务（定时任务库的管理接口）

★ 每个请求先过 OpsGuard 验签名，只读的也验。宿主自己的运营接口用 ops.dependency() 挂进来。
★ 被拒绝（前置条件不满足）回 409，运营中心按「要人看」处理，不反复重试。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from atlas_appconfig import AppConfig, ConfigRoutes, ConfigScope
from atlas_jobs import JobRoutes, JobScheduler
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .guard import OpsGuard
from .lifecycle import TenantLifecycleService
from .state import Rejected

__all__ = ["OpsRoutes"]

PREFIX = "/ops/v1"


class TenantStateOut(BaseModel):
    tenant_uuid: UUID
    status: str
    operation_uuid: UUID | None
    changed_at: datetime


class OpsRoutes:
    def __init__(
        self,
        guard: OpsGuard,
        lifecycle: TenantLifecycleService,
        *,
        config: AppConfig | None = None,
        jobs: JobScheduler | None = None,
        instance: str | None = None,
    ) -> None:
        """instance：系统任务接口里显示的实例标识（默认主机名），和宿主别处用的保持一致。"""
        self._guard = guard
        self._lifecycle = lifecycle
        self.router = APIRouter(prefix=PREFIX, tags=["ops"])
        for action in ("suspend", "resume", "purge"):
            self.router.add_api_route(
                f"/tenants/{{tenant_uuid}}/{action}", self._handler(action), methods=["POST"]
            )
        self.router.add_api_route(
            "/tenant-states", self.states, methods=["GET"], response_model=list[TenantStateOut]
        )
        if config is not None:
            self.router.include_router(
                ConfigRoutes(config, guard, scope=ConfigScope.SYSTEM, prefix="/configs").router
            )
        if jobs is not None:
            self.router.include_router(
                JobRoutes(jobs, guard, prefix="/jobs", instance=instance).router
            )

    def dependency(self) -> Any:
        """宿主自己的运营接口挂上它：先验签名，拿到 OperatorStamp。"""
        return self._guard.dependency()

    def _handler(self, action: str) -> Any:
        async def handle(tenant_uuid: UUID, request: Request) -> dict[str, Any]:
            stamp = await self._guard.verify(request)
            try:
                return await getattr(self._lifecycle, action)(tenant_uuid, stamp)
            except Rejected as exc:
                raise HTTPException(409, str(exc)) from exc

        handle.__name__ = f"tenant_{action}"
        return handle

    async def states(self, request: Request) -> list[TenantStateOut]:
        await self._guard.verify(request)
        return [
            TenantStateOut(
                tenant_uuid=s.tenant_uuid,
                status=s.status.value,
                operation_uuid=s.operation_uuid,
                changed_at=s.changed_at,
            )
            for s in await self._lifecycle.states()
        ]
