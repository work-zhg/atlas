"""租户管理（平台级，租户设计 §07、§11）。

★ 两道闸：操作码 tenant:view / tenant:manage（只对平台租户内的人生效，
  见 effective.authz_snapshot），再加 require_platform —— 当前租户必须是平台租户。
★ 平台管理员在这里只碰租户本身与开通关系，看不到客户租户内的人员与授权。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from ..common import iso
from ..db.models import Tenant
from ..tenant.service import TenantService, require_platform, tenant_usable
from .deps import Principal, SessionDep, SettingsDep, require

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


class AdminIn(BaseModel):
    account: str
    name: str
    email: str


class TenantCreate(BaseModel):
    code: str
    name: str
    admin: AdminIn
    max_users: int | None = None
    expires_at: datetime | None = None


class TenantUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    max_users: int | None = None
    expires_at: datetime | None = None


class AppSwitch(BaseModel):
    enabled: bool


def tenant_view(t: Tenant, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(t.uuid),
        "code": t.code,
        "name": t.name,
        "kind": t.kind,
        "status": t.status,
        "usable": tenant_usable(t),
        "max_users": t.max_users,
        "expires_at": iso(t.expires_at) if t.expires_at else None,
        "created_at": iso(t.created_at),
        "version": t.version,
        **extra,
    }


@router.get("")
async def list_tenants(
    session: SessionDep, p: Principal = require("tenant:view")
) -> list[dict[str, Any]]:
    await require_platform(session)
    return [tenant_view(t, users=n) for t, n in await TenantService(session, p.actor).list()]


@router.post("", status_code=201)
async def create_tenant(
    body: TenantCreate,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("tenant:manage"),
) -> dict[str, Any]:
    """建租户、根部门与首个租户超级管理员。临时密码只在这次响应里出现。"""
    await require_platform(session)
    tenant, admin, temp = await TenantService(session, p.actor).create(
        code=body.code,
        name=body.name,
        admin=body.admin.model_dump(),
        max_users=body.max_users,
        expires_at=body.expires_at,
        settings=settings,
    )
    return {
        "tenant": tenant_view(tenant, users=1),
        "admin": {"id": str(admin.uuid), "account": admin.account, "name": admin.name},
        "temp_password": temp,
    }


@router.patch("/{tenant_id}")
async def update_tenant(
    tenant_id: UUID,
    body: TenantUpdate,
    session: SessionDep,
    p: Principal = require("tenant:manage"),
) -> dict[str, Any]:
    await require_platform(session)
    data = body.model_dump(exclude_unset=True)
    return tenant_view(await TenantService(session, p.actor).update(tenant_id, data))


@router.get("/{tenant_id}/apps")
async def tenant_apps(
    tenant_id: UUID, session: SessionDep, p: Principal = require("tenant:view")
) -> list[dict[str, Any]]:
    await require_platform(session)
    return [
        {
            "app": {"id": str(a.uuid), "name": a.name, "icon": a.icon, "status": a.status},
            "enabled": bool(ta and ta.status == "active"),
            "scope_all": bool(ta and ta.scope_all),
            "enabled_at": iso(ta.enabled_at) if ta else None,
        }
        for a, ta in await TenantService(session, p.actor).apps(tenant_id)
    ]


@router.put("/{tenant_id}/apps/{app_id}")
async def switch_app(
    tenant_id: UUID,
    app_id: UUID,
    body: AppSwitch,
    session: SessionDep,
    p: Principal = require("tenant:manage"),
) -> dict[str, bool]:
    await require_platform(session)
    await TenantService(session, p.actor).set_app(tenant_id, app_id, body.enabled)
    return {"ok": True}
