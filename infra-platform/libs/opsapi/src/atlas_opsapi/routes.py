"""运营接口的路由，统一挂在 /ops/v1（网关不转发这个前缀）。

    PUT    /ops/v1/tenants/{tenant_uuid}    创建：body 是开通时给本系统填的参数（JSON 对象）
    DELETE /ops/v1/tenants/{tenant_uuid}    删一批：{deleted, remaining}；remaining 为真就再调一次
    /ops/v1/configs                         系统参数（应用配置库的管理接口，scope=SYSTEM）
    /ops/v1/jobs                            系统任务（定时任务库的管理接口）

★ 每个请求先过 OpsGuard 验签名，只读的也验。宿主自己的运营接口用 ops.dependency() 挂进来。
★ 被拒绝（参数错、前置条件不满足）回 409，运营中心按「要人看」处理，不反复重试。
★ 没有停用、恢复：业务系统不知道租户状态（见 lifecycle.py）。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from atlas_appconfig import AppConfig, ConfigRoutes, ConfigScope
from atlas_jobs import JobRoutes, JobScheduler
from atlas_tenancy import PurgeReport, TenantScope
from fastapi import APIRouter, FastAPI, HTTPException, Request

from .guard import OpsGuard
from .lifecycle import Rejected, TenantLifecycle

__all__ = ["PREFIX", "OpsRoutes"]

logger = logging.getLogger(__name__)

PREFIX = "/ops/v1"


def _report_out(report: PurgeReport) -> dict[str, Any]:
    return {"deleted": dict(report.deleted), "remaining": report.remaining}


class OpsRoutes:
    def __init__(
        self,
        guard: OpsGuard,
        lifecycle: TenantLifecycle,
        *,
        config: AppConfig | None = None,
        jobs: JobScheduler | None = None,
        instance: str | None = None,
    ) -> None:
        """instance：系统任务接口里显示的实例标识（默认主机名），和宿主别处用的保持一致。"""
        self._guard = guard
        self._lifecycle = lifecycle
        self.router = APIRouter(prefix=PREFIX, tags=["ops"])
        self.router.add_api_route("/tenants/{tenant_uuid}", self.create, methods=["PUT"])
        self.router.add_api_route("/tenants/{tenant_uuid}", self.delete, methods=["DELETE"])
        if config is not None:
            self.router.include_router(
                ConfigRoutes(config, guard, scope=ConfigScope.SYSTEM, prefix="/configs").router
            )
        if jobs is not None:
            self.router.include_router(
                JobRoutes(jobs, guard, prefix="/jobs", instance=instance).router
            )

    @classmethod
    def mount(
        cls,
        app: FastAPI,
        guard: OpsGuard,
        lifecycle: TenantLifecycle,
        *,
        config: AppConfig | None = None,
        jobs: JobScheduler | None = None,
        instance: str | None = None,
    ) -> OpsRoutes:
        """建好并挂到宿主上；返回它，宿主自己的运营接口可以接着挂进 ops.router。"""
        ops = cls(guard, lifecycle, config=config, jobs=jobs, instance=instance)
        app.include_router(ops.router)
        return ops

    def dependency(self) -> Any:
        """宿主自己的运营接口挂上它：先验签名，拿到 OperatorStamp。"""
        return self._guard.dependency()

    async def create(self, tenant_uuid: UUID, request: Request) -> dict[str, Any]:
        stamp = await self._guard.verify(request)
        params = _params(await request.body())
        logger.info(
            "运营接口：创建租户 %s（%s，步骤 %s）", tenant_uuid, stamp.who, stamp.idempotency_key
        )
        try:
            with TenantScope.enter(tenant_uuid):
                await self._lifecycle.create(tenant_uuid, params)
        except Rejected as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"tenant_uuid": str(tenant_uuid)}

    async def delete(self, tenant_uuid: UUID, request: Request) -> dict[str, Any]:
        stamp = await self._guard.verify(request)
        logger.info(
            "运营接口：删除租户 %s 的数据（%s，步骤 %s）",
            tenant_uuid,
            stamp.who,
            stamp.idempotency_key,
        )
        try:
            report = await self._lifecycle.delete(tenant_uuid)
        except Rejected as exc:
            raise HTTPException(409, str(exc)) from exc
        return _report_out(report)


def _params(body: bytes) -> dict[str, Any]:
    if not body.strip():
        return {}
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise HTTPException(422, "创建参数不是合法的 JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "创建参数必须是 JSON 对象")
    return value
