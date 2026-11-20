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

★ 停用：管理接口、内部接口、MCP 一律 403；入库、重建开始前查租户状态，停用的不开始，过一会儿再
  看（tasks/indexing.py），恢复后接着入。正在入的那一份做完为止。
★ 删除：先删外部资源 —— 向量库里这个租户的 collection、对象存储里这个租户的文件 —— 再分批删
  数据库里的行。对象只删知识库自己的子目录（{租户}/_knowledge/），不删整个 {租户}/：桶和 Atlas
  的工作空间、技能共用。多租户之前建的库在桶根 _knowledge/{库} 下，按库记着的目录逐个删。
  用户中心里这些库的授权随租户在用户中心一起删。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from atlas_jobs import JobScheduler
from atlas_opsapi import OperatorStamp, OpsGuard, OpsRoutes, TenantGate, TenantLifecycleService
from atlas_tenancy import PurgeReport, SystemReason, TenantDataPurge, TenantNaming, TenantScope
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..capacity import MB, KnowledgeCapacity, TenantStorage
from ..db.models import (
    LIVE_INDEX_STATUSES,
    Base,
    ChildChunk,
    Dataset,
    DatasetPermission,
    Document,
    IndexVersion,
    KnowledgeSet,
    MetadataField,
    ProcessRule,
    QueryLog,
    Retrieval,
    Segment,
    TenantCapacity,
)
from ..db.session import get_sessionmaker
from ..runtime import Runtime
from ..settings import get_settings
from ..storage import StorageNotConfigured

__all__ = ["mount_ops"]

logger = logging.getLogger(__name__)

#: 删除租户时按这个顺序分批删（子表在前）
PURGE_ORDER = (
    ChildChunk,
    Segment,
    QueryLog,
    Retrieval,
    MetadataField,
    IndexVersion,
    Document,
    ProcessRule,
    DatasetPermission,
    Dataset,
    KnowledgeSet,
    TenantCapacity,
)


class KnowledgeTenantLifecycle:
    """知识库的租户生命周期。钩子都在这个租户的上下文里调（TenantLifecycleService）。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def on_suspend(self, tenant_uuid: UUID) -> None:
        return None

    async def on_resume(self, tenant_uuid: UUID) -> None:
        return None

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        rt: Runtime = self._app.state.runtime
        report = PurgeReport()
        async with get_sessionmaker()() as session:
            datasets = list(await session.scalars(select(Dataset)))
            versions = await session.scalars(
                select(IndexVersion.collection_name).where(
                    IndexVersion.status.in_(LIVE_INDEX_STATUSES)
                )
            )
            collections = {d.collection_name for d in datasets} | set(versions)
        for name in sorted(collections):
            await rt.vectors.for_collection(name).delete()
        if collections:
            report.deleted["collection"] = len(collections)

        own = TenantNaming.object_prefix(rt.settings.oss_prefix) + "/"
        legacy = [f"{d.storage_prefix}/" for d in datasets if not d.storage_prefix.startswith(own)]
        try:
            storage = rt.storage
        except StorageNotConfigured:
            return report  # 没配对象存储：没有文件要删

        def _delete() -> int:
            return sum(storage.delete_prefix(prefix) for prefix in [own, *legacy])

        deleted = await asyncio.to_thread(_delete)
        if deleted:
            report.deleted["object"] = deleted
        return report


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
    from ..capacity import DEFAULT_STORAGE_MB

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
