"""运营接口（/ops/v1）：租户的停用、恢复、删除，容量，系统参数，系统任务。只给运营中心（先验签名）。

    POST /ops/v1/tenants/{tenant}/suspend|resume|purge   运营接口库给的
    GET  /ops/v1/tenant-states
    GET  /ops/v1/configs …                                系统参数（应用配置库，如默认份额）
    GET  /ops/v1/jobs …                                   系统任务（原来的 /v1/jobs）
    GET  /ops/v1/capacity                                 全平台的知识库文件用量
    GET  /ops/v1/quotas                                   默认份额，和单独定过份额的租户
    PUT  /ops/v1/quotas/default                           改默认份额
    PUT  /ops/v1/quotas/{tenant}                          改一个租户的份额；use_default = 回到默认
    GET  /ops/v1/usage/{tenant}                           一个租户的份额和用量

★ 容量只有存储（知识库文件）一项，接口形状和 Atlas 的一样（运营中心按同一套契约调）；
  份额 null = 不限。

★ 停用、删除租户时知识库做什么：见 ops/lifecycle.py。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from atlas_jobs import JobScheduler
from atlas_opsapi import OperatorStamp, OpsGuard, OpsRoutes, TenantGate, TenantLifecycleService
from atlas_tenancy import SystemReason, TenantDataPurge, TenantScope
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..db.models import Base, Document, TenantCapacity
from ..db.session import get_sessionmaker
from ..ops.capacity import MB, KnowledgeCapacity, TenantStorage
from ..ops.lifecycle import PURGE_ORDER, KnowledgeTenantLifecycle
from ..settings import get_settings

__all__ = ["mount_ops"]

logger = logging.getLogger(__name__)


def mount_ops(app: FastAPI, jobs: JobScheduler) -> None:
    settings = get_settings()
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    sessions = get_sessionmaker()
    app.state.ops_guard = guard = OpsGuard(key)
    app.state.tenant_gate = gate = TenantGate(sessions)
    # 存储份额：上传前判（runtime 在 lifespan 里接上它），默认份额是系统参数
    app.state.capacity = capacity = KnowledgeCapacity(sessions)
    purge = TenantDataPurge(PURGE_ORDER, base=Base)
    lifecycle = TenantLifecycleService(sessions, KnowledgeTenantLifecycle(app), gate, purge)
    ops = OpsRoutes(guard, lifecycle, config=capacity.config, jobs=jobs)
    ops.router.include_router(_capacity_router)
    app.include_router(ops.router)


# ───────────────────────────── 容量


class QuotaIn(BaseModel):
    """null = 不限。"""

    storage_bytes: int | None = Field(default=None, ge=0)


class TenantQuotaIn(QuotaIn):
    #: 回到默认份额（之后改默认份额照样跟着变）；为真时忽略 storage_bytes
    use_default: bool = False


class QuotaOut(BaseModel):
    storage_bytes: int | None


class TenantQuotaOut(QuotaOut):
    tenant_uuid: UUID
    updated_by: str
    updated_at: datetime


class QuotasOut(BaseModel):
    default: QuotaOut
    tenants: list[TenantQuotaOut]


class UsageOut(BaseModel):
    storage_bytes: int


class CapacityOut(BaseModel):
    usage: UsageOut


class TenantUsageOut(BaseModel):
    tenant_uuid: UUID
    quota: QuotaOut
    is_default: bool
    usage: UsageOut


async def _operator(request: Request) -> OperatorStamp:
    return await request.app.state.ops_guard.verify(request)  # type: ignore[no-any-return]


Operator = Annotated[OperatorStamp, Depends(_operator)]


def _cap(request: Request) -> KnowledgeCapacity:
    return request.app.state.capacity  # type: ignore[no-any-return]


Capacity = Annotated[KnowledgeCapacity, Depends(_cap)]

_capacity_router = APIRouter()


def _usage_out(u: TenantStorage) -> TenantUsageOut:
    return TenantUsageOut(
        tenant_uuid=u.tenant_uuid,
        quota=QuotaOut(storage_bytes=u.quota_bytes),
        is_default=u.is_default,
        usage=UsageOut(storage_bytes=u.used_bytes),
    )


@_capacity_router.get("/capacity", response_model=CapacityOut)
async def capacity_total(_: Operator) -> CapacityOut:
    with TenantScope.system(SystemReason.OPS):
        async with get_sessionmaker()() as session:
            used = await session.scalar(select(func.coalesce(func.sum(Document.file_size), 0)))
    return CapacityOut(usage=UsageOut(storage_bytes=int(used or 0)))


@_capacity_router.get("/quotas", response_model=QuotasOut)
async def quotas(_: Operator, cap: Capacity) -> QuotasOut:
    with TenantScope.system(SystemReason.OPS):
        async with get_sessionmaker()() as session:
            rows = list(await session.scalars(select(TenantCapacity)))
    return QuotasOut(
        default=QuotaOut(storage_bytes=await cap.default_bytes()),
        tenants=[
            TenantQuotaOut(
                tenant_uuid=r.tenant_uuid,
                storage_bytes=r.storage_bytes,
                updated_by=r.updated_by,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
    )


@_capacity_router.put("/quotas/default", response_model=QuotaOut)
async def set_default(body: QuotaIn, op: Operator, cap: Capacity) -> QuotaOut:
    """默认份额是系统参数 capacity.default_storage_mb：经应用配置库改（有版本和修订历史）。"""
    from ..ops.capacity import DEFAULT_STORAGE_MB

    entry = await cap.config.entry(DEFAULT_STORAGE_MB.name)
    mb = None if body.storage_bytes is None else body.storage_bytes // MB
    await cap.config.set(
        DEFAULT_STORAGE_MB.name, mb or None, expected_version=entry.item.version, by=op.who
    )
    return QuotaOut(storage_bytes=await cap.default_bytes())


@_capacity_router.put("/quotas/{tenant_uuid}", response_model=TenantUsageOut)
async def set_quota(
    tenant_uuid: UUID, body: TenantQuotaIn, op: Operator, cap: Capacity
) -> TenantUsageOut:
    with TenantScope.enter(tenant_uuid):
        async with get_sessionmaker()() as session:
            if body.use_default:
                await cap.use_default(session)
            else:
                await cap.set_quota(session, body.storage_bytes, by=op.who)
            await session.commit()
            return _usage_out(await cap.usage(session))


@_capacity_router.get("/usage/{tenant_uuid}", response_model=TenantUsageOut)
async def usage(tenant_uuid: UUID, _: Operator, cap: Capacity) -> TenantUsageOut:
    with TenantScope.enter(tenant_uuid):
        async with get_sessionmaker()() as session:
            return _usage_out(await cap.usage(session))
