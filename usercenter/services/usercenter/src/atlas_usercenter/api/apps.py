"""应用接入接口（租户视角）：已开通的应用、可访问范围，操作 / 菜单 / 数据编码（只读），角色。

★ 应用目录（接入应用、凭据、启停、操作、菜单、数据编码、应用内置角色）归运营中心，
  经运营接口维护（api/ops.py，租户隔离设计 §3.3）；这里只读，租户里谁都改不了。
  租户能改的只有本租户的可访问范围和本租户自定义的角色。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from ..app.service import AppService
from ..db.models import User
from ..errors import Forbidden, NotFound
from ..perm.effective import can_access, effective_roles, role_holders, visible_menu_tree
from .deps import Principal, PrincipalDep, SessionDep, SettingsDep, require
from .views import app_view, data_type_view, menu_view, op_view, role_view

router = APIRouter(prefix="/api/v1", tags=["apps"])


def _can_view_meta(p: Principal) -> None:
    """操作目录、菜单、角色：role:view 或 tenant_app:manage 可看（维护角色需要看到它们）。"""
    if not (p.has("role:view") or p.has("tenant_app:manage")):
        raise Forbidden("PERMISSION_DENIED", "缺少操作权限：role:view 或 tenant_app:manage")


class ScopeBody(BaseModel):
    all: bool
    dept_ids: list[UUID] = []


class RoleCreate(BaseModel):
    name: str
    code: str
    description: str | None = None
    copy_from: UUID | None = None


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class IdsBody(BaseModel):
    ids: list[UUID]


# ───────────────────────────── 应用


async def _tenant_view(svc: AppService, a: Any, p: Principal) -> dict[str, Any] | None:
    """本租户视角：是否开通、范围。看不到本租户没开通的应用（返回 None）。"""
    opened = await svc.tenant_app(a.uuid)
    enabled = a.is_builtin or (opened is not None and opened.status == "active")
    if not enabled:
        return None
    extra: dict[str, Any] = {
        "enabled": enabled,
        "scope_all": a.is_builtin or bool(opened and opened.scope_all),
    }
    if p.has("tenant_app:manage"):
        extra["accessible_users"] = await svc.accessible_users(a)
        extra["scope_dept_ids"] = [str(d) for d in await svc.scope_depts(a.uuid)]
    return extra


@router.get("/apps")
async def list_apps(session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    out = []
    for a in await svc.list():
        tv = await _tenant_view(svc, a, p)
        if tv is None:
            continue
        extra: dict[str, Any] = {**await svc.counts(a.uuid), **tv}
        # 应用凭据只给运营中心
        out.append({**app_view(a, **extra), "app_key": None, "secret_tail": None})
    return out


@router.get("/apps/{app_id}")
async def get_app(app_id: UUID, session: SessionDep, p: PrincipalDep) -> dict[str, Any]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    app = await svc.get(app_id)
    tv = await _tenant_view(svc, app, p)
    if tv is None:
        raise NotFound("APP_NOT_FOUND", "应用不存在")
    view = app_view(app, **await svc.counts(app.uuid), **tv)
    return {**view, "app_key": None, "secret_tail": None}


@router.put("/apps/{app_id}/scope")
async def set_scope(
    app_id: UUID,
    body: ScopeBody,
    session: SessionDep,
    p: Principal = require("tenant_app:manage"),
) -> dict[str, Any]:
    """本租户内的可访问范围（租户设计 §04）—— 租户自己的事，不需要平台权限。"""
    n = await AppService(session, p.actor).set_scope(app_id, body.all, body.dept_ids)
    return {"accessible_users": n}


# ───────────────────────────── 操作


@router.get("/apps/{app_id}/operations")
async def list_ops(app_id: UUID, session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    return [op_view(o, roles=await svc.op_roles(o.uuid)) for o in await svc.operations(app_id)]


# ───────────────────────────── 菜单


@router.get("/apps/{app_id}/menus")
async def list_menus(app_id: UUID, session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    counts = await svc.menu_role_counts(app_id)
    return [menu_view(m, role_count=counts.get(m.uuid, 0)) for m in await svc.menus(app_id)]


@router.get("/apps/{app_id}/menus/preview")
async def preview_menus(
    app_id: UUID, user_id: UUID, session: SessionDep, p: PrincipalDep
) -> dict[str, Any]:
    """按用户预览可见菜单（与开放接口返回的 menus 一致）。"""
    _can_view_meta(p)
    app = await AppService(session, p.actor).get(app_id)
    user = (await session.execute(select(User).where(User.uuid == user_id))).scalar_one()
    access = await can_access(session, user, app)
    eff = await effective_roles(session, user) if access else {}
    mine = {k: v for k, v in eff.items() if v.role.app_uuid == app.uuid}
    tree, visible = (
        await visible_menu_tree(session, app.uuid, list(mine)) if access else ([], set())
    )
    return {
        "can_access": access,
        "roles": [v.role.name for v in mine.values()],
        "menus": tree,
        "visible_ids": [str(v) for v in visible],
    }


# ───────────────────────────── 角色


@router.get("/apps/{app_id}/roles")
async def list_roles(app_id: UUID, session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    out = []
    for r in await svc.roles(app_id):
        holders = await role_holders(session, r.uuid)
        active = (
            len(
                list(
                    await session.scalars(
                        select(User.uuid).where(
                            User.uuid.in_(list(holders)), User.status == "active"
                        )
                    )
                )
            )
            if holders
            else 0
        )
        out.append(
            role_view(r, operation_count=len(await svc.role_ops(r.uuid)), holder_count=active)
        )
    return out


@router.post("/apps/{app_id}/roles", status_code=201)
async def create_role(
    app_id: UUID, body: RoleCreate, session: SessionDep, p: Principal = require("role:manage")
) -> dict[str, Any]:
    r = await AppService(session, p.actor).create_role(
        app_id, body.name, body.code, body.description, body.copy_from
    )
    return role_view(r)


@router.get("/roles/{role_id}")
async def get_role(role_id: UUID, session: SessionDep, p: PrincipalDep) -> dict[str, Any]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    r = await svc.get_role(role_id)
    return role_view(
        r,
        operation_ids=[str(x) for x in await svc.role_ops(r.uuid)],
        menu_ids=[str(x) for x in await svc.role_menus(r.uuid)],
    )


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: UUID, body: RoleUpdate, session: SessionDep, p: Principal = require("role:manage")
) -> dict[str, Any]:
    return role_view(
        await AppService(session, p.actor).update_role(role_id, body.model_dump(exclude_unset=True))
    )


@router.put("/roles/{role_id}/operations")
async def set_role_ops(
    role_id: UUID, body: IdsBody, session: SessionDep, p: Principal = require("role:manage")
) -> dict[str, Any]:
    return role_view(await AppService(session, p.actor).set_role_operations(role_id, body.ids))


@router.put("/roles/{role_id}/menus")
async def set_role_menus(
    role_id: UUID, body: IdsBody, session: SessionDep, p: Principal = require("role:manage")
) -> dict[str, Any]:
    return role_view(await AppService(session, p.actor).set_role_menus(role_id, body.ids))


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: UUID, session: SessionDep, dry_run: bool = False, p: Principal = require("role:manage")
) -> dict[str, Any]:
    return await AppService(session, p.actor).delete_role(role_id, dry_run)


# ───────────────────────────── 数据编码


@router.get("/apps/{app_id}/data-types")
async def list_data_types(
    app_id: UUID, session: SessionDep, p: PrincipalDep
) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    return [
        data_type_view(d, data_count=await svc.data_type_count(d.uuid))
        for d in await svc.data_types(app_id)
    ]


@router.get("/data-types")
async def all_data_types(session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    """数据授权页的筛选项：所有登录用户可见（只返回编码与名称）。"""
    return [data_type_view(d) for d in await AppService(session, p.actor).data_types()]


_ = SettingsDep
