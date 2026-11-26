"""租户：列表、开通、停用、恢复、关闭、补发首个管理员的临时密码，以及操作进度（tenants.py）。
给新登记的系统补建在 api/systems.py。

★ 都属于租户模块（OpsTenant）：看要只读；开通、停用、恢复、改、立即重试要读写；关闭（删数据）、
  补发临时密码要 Owner。

★ 租户名单、名字、状态都问用户中心；运营库只存「对租户做过什么、做到哪了」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ..tenants import OpenRequest, TenantOps
from .deps import ClientDep, SessionDep, TenantOwnerDep, TenantReadDep, TenantWriteDep

router = APIRouter(prefix="/api", tags=["tenants"])


class AdminIn(BaseModel):
    account: str
    name: str
    email: str


class OpenIn(BaseModel):
    code: str
    name: str
    admin: AdminIn
    max_users: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    apps: list[str] = []
    #: 各系统的创建参数 {系统编码: 参数}；下面两项是控制台的便捷写法，开通时并进来
    params: dict[str, dict[str, Any]] = {}
    #: {系统编码: 份额}，如 {"atlas": {"running_runs": 20, "sandboxes": 30, "storage_bytes": …}}
    quotas: dict[str, dict[str, Any]] = {}
    #: 技能应用装不装技能市场里「默认安装」的技能
    install_default_skills: bool = True


class TenantPatch(BaseModel):
    name: str | None = None
    max_users: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class SuspendIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class CloseIn(BaseModel):
    confirm_code: str


@router.get("/tenants")
async def list_tenants(
    p: TenantReadDep, session: SessionDep, client: ClientDep
) -> list[dict[str, Any]]:
    return await TenantOps(session, client).list(p.who)


@router.post("/tenants", status_code=201)
async def open_tenant(
    body: OpenIn, response: Response, p: TenantWriteDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    """开通租户。第一步（用户中心建租户和首个管理员）同步跑：临时密码只在这次响应里出现。"""
    req = OpenRequest(
        code=body.code,
        name=body.name,
        admin=body.admin.model_dump(),
        max_users=body.max_users,
        expires_at=body.expires_at,
        apps=body.apps,
        params=body.params,
        quotas=body.quotas,
        install_default_skills=body.install_default_skills,
    )
    out = await TenantOps(session, client).open(req, p.who)
    if out["tenant"] is None:
        response.status_code = 202
    return out


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: UUID, p: TenantReadDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    ops = TenantOps(session, client)
    return {
        "tenant": await ops.get(tenant_id, p.who),
        "operations": await ops.operations(tenant_id),
    }


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: UUID, body: TenantPatch, p: TenantWriteDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True, mode="json")
    return await TenantOps(session, client).update(tenant_id, data, p.who)


@router.post("/tenants/{tenant_id}/suspend", status_code=202)
async def suspend_tenant(
    tenant_id: UUID, body: SuspendIn, p: TenantWriteDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    return await TenantOps(session, client).suspend(tenant_id, body.reason, p.who)


@router.post("/tenants/{tenant_id}/resume", status_code=202)
async def resume_tenant(
    tenant_id: UUID, p: TenantWriteDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    return await TenantOps(session, client).resume(tenant_id, p.who)


@router.post("/tenants/{tenant_id}/close", status_code=202)
async def close_tenant(
    tenant_id: UUID, body: CloseIn, p: TenantOwnerDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    """关闭并删除这个租户的全部数据。只能关已停用的，要输入企业代码确认。"""
    return await TenantOps(session, client).close(tenant_id, body.confirm_code, p.who)


@router.post("/tenants/{tenant_id}/admin-password")
async def reissue_admin_password(
    tenant_id: UUID, p: TenantOwnerDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    """首个管理员还没登录过时补发临时密码（旧的作废）。只在这次响应里出现。"""
    return await TenantOps(session, client).reissue_admin_password(tenant_id, p.who)


@router.get("/operations")
async def running_operations(
    _: TenantReadDep, session: SessionDep, client: ClientDep
) -> list[dict[str, Any]]:
    """进行中的租户操作（首页看哪些卡住了）。"""
    return await TenantOps(session, client).running()


@router.post("/operations/{operation_id}/retry")
async def retry_operation(
    operation_id: UUID, p: TenantWriteDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    """失败的步骤立即重做（不等退避）。"""
    return await TenantOps(session, client).retry_now(operation_id, p.who)
