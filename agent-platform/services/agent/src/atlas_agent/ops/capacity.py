"""容量契约在 Atlas 的实现（c4-model/agent/code-ops.html · CapacityOps）。

    GET  /ops/v1/capacity                 总容量（每个实例的并发槽位）和全平台的用量
    GET  /ops/v1/quotas                   默认份额，和单独定过份额的租户
    PUT  /ops/v1/quotas/default           改默认份额（系统参数 capacity.default_*）
    PUT  /ops/v1/quotas/{tenant_uuid}     改一个租户的份额；use_default = 回到默认份额
    GET  /ops/v1/usage/{tenant_uuid}      一个租户的份额和用量

★ 只有数字，不含任何租户的业务内容。份额的 null = 不限。
★ 存储在接口里一律按字节；默认份额存在应用配置库里按 MB（向下取整）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from atlas_opsapi import OperatorStamp
from atlas_tenancy import SystemReason, TenantScope
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..capacity import CapacityService, Quota
from ..db.models import Run, StorageUsageRow, TenantCapacityRow, UserSandboxRow
from ..db.session import get_sessionmaker
from ..deps import SettingsDep

router = APIRouter(tags=["ops"])

_MB = 1024 * 1024


async def _operator(request: Request) -> OperatorStamp:
    return await request.app.state.ops_guard.verify(request)  # type: ignore[no-any-return]


Operator = Annotated[OperatorStamp, Depends(_operator)]


def _capacity(request: Request) -> CapacityService:
    return request.app.state.capacity  # type: ignore[no-any-return]


Capacity = Annotated[CapacityService, Depends(_capacity)]


class QuotaIn(BaseModel):
    """null = 不限。"""

    running_runs: int | None = Field(default=None, ge=0)
    sandboxes: int | None = Field(default=None, ge=0)
    storage_bytes: int | None = Field(default=None, ge=0)


class TenantQuotaIn(QuotaIn):
    #: 回到默认份额（之后改默认份额照样跟着变）；为真时忽略其余字段
    use_default: bool = False


class QuotaOut(QuotaIn):
    pass


class TenantQuotaOut(QuotaOut):
    tenant_uuid: UUID
    is_default: bool
    updated_by: str | None = None
    updated_at: datetime | None = None


class QuotasOut(BaseModel):
    default: QuotaOut
    tenants: list[TenantQuotaOut]


class UsageOut(BaseModel):
    running_runs: int
    sandboxes: int
    storage_bytes: int


class TenantUsageOut(BaseModel):
    tenant_uuid: UUID
    quota: QuotaOut
    is_default: bool
    usage: UsageOut


class CapacityOut(BaseModel):
    #: 每个执行器实例同时执行的 run 上限（总容量 = 它 × 执行器实例数，实例数看部署）
    runs_per_instance: int
    #: 全平台现在的用量
    usage: UsageOut


def _quota_out(q: Quota) -> QuotaOut:
    return QuotaOut(
        running_runs=q.running_runs, sandboxes=q.sandboxes, storage_bytes=q.storage_bytes
    )


@router.get("/capacity", response_model=CapacityOut)
async def capacity(_: Operator, settings: SettingsDep) -> CapacityOut:
    now = datetime.now(UTC)
    with TenantScope.system(SystemReason.OPS):
        async with get_sessionmaker()() as session:
            runs = await session.scalar(
                sa.select(sa.func.count(Run.id)).where(
                    Run.status == "running", Run.owner.isnot(None), Run.lease_until > now
                )
            )
            sandboxes = await session.scalar(
                sa.select(sa.func.count(UserSandboxRow.id)).where(
                    UserSandboxRow.status.in_(("creating", "ready"))
                )
            )
            storage = await session.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(StorageUsageRow.bytes), 0))
            )
    return CapacityOut(
        runs_per_instance=settings.max_concurrent_runs,
        usage=UsageOut(
            running_runs=runs or 0, sandboxes=sandboxes or 0, storage_bytes=int(storage or 0)
        ),
    )


@router.get("/quotas", response_model=QuotasOut)
async def quotas(_: Operator, request: Request) -> QuotasOut:
    runs, sandboxes, storage = await request.app.state.runtime_config.capacity_defaults()
    default = QuotaOut(
        running_runs=runs or None, sandboxes=sandboxes or None, storage_bytes=storage or None
    )
    with TenantScope.system(SystemReason.OPS):
        async with get_sessionmaker()() as session:
            rows = list(
                await session.scalars(
                    sa.select(TenantCapacityRow).where(TenantCapacityRow.is_default.is_(False))
                )
            )
    return QuotasOut(
        default=default,
        tenants=[
            TenantQuotaOut(
                tenant_uuid=r.tenant_uuid,
                running_runs=r.running_runs,
                sandboxes=r.sandboxes,
                storage_bytes=r.storage_bytes,
                is_default=False,
                updated_by=r.updated_by,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
    )


@router.put("/quotas/default", response_model=QuotaOut)
async def set_default(body: QuotaIn, op: Operator, request: Request) -> QuotaOut:
    """默认份额是三个系统参数：经应用配置库改（有版本和修订历史）。"""
    config = request.app.state.runtime_config
    keys = config.keys
    values = (
        (keys.default_running_runs, body.running_runs),
        (keys.default_sandboxes, body.sandboxes),
        (
            keys.default_storage_mb,
            None if body.storage_bytes is None else body.storage_bytes // _MB,
        ),
    )
    for key, value in values:
        entry = await config.config.entry(key.name)
        await config.config.set(
            key.name, value or None, expected_version=entry.item.version, by=op.who
        )
    runs, sandboxes, storage = await config.capacity_defaults()
    return QuotaOut(
        running_runs=runs or None, sandboxes=sandboxes or None, storage_bytes=storage or None
    )


@router.put("/quotas/{tenant_uuid}", response_model=TenantUsageOut)
async def set_quota(
    tenant_uuid: UUID, body: TenantQuotaIn, op: Operator, capacity: Capacity
) -> TenantUsageOut:
    quota = (
        None if body.use_default else Quota(body.running_runs, body.sandboxes, body.storage_bytes)
    )
    with TenantScope.enter(tenant_uuid):
        async with get_sessionmaker()() as session:
            await capacity.set_quota(session, quota, by=op.who)
            await session.commit()
        return await _usage(capacity)


@router.get("/usage/{tenant_uuid}", response_model=TenantUsageOut)
async def usage(tenant_uuid: UUID, _: Operator, capacity: Capacity) -> TenantUsageOut:
    with TenantScope.enter(tenant_uuid):
        return await _usage(capacity)


async def _usage(capacity: CapacityService) -> TenantUsageOut:
    async with get_sessionmaker()() as session:
        u = await capacity.usage(session)
    return TenantUsageOut(
        tenant_uuid=u.tenant_uuid,
        quota=_quota_out(u.quota),
        is_default=u.is_default,
        usage=UsageOut(
            running_runs=u.running_runs, sandboxes=u.sandboxes, storage_bytes=u.storage_bytes
        ),
    )
