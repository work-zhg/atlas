"""用户管理接口。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..common import Page, iso
from ..db.models import App, DataObject, DataType, Dept
from ..org.service import OrgService
from ..perm.data import DataService, level_name
from ..perm.effective import authz_snapshot, can_access, effective_roles
from ..perm.service import GrantService
from ..perm.views import positions_of
from ..user.service import UserService
from .deps import Principal, SessionDep, SettingsDep, require
from .views import app_view, user_brief, user_view

router = APIRouter(prefix="/api/v1/users", tags=["users"])


class UserCreate(BaseModel):
    name: str
    account: str
    email: str
    phone: str | None = None
    dept_id: UUID


class UserUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    dept_id: UUID | None = None
    version: int | None = None


class DisableBody(BaseModel):
    reason: str | None = None


async def _dept_paths(session: SessionDep) -> dict[UUID, list[str]]:
    depts = {d.uuid: d for d in await session.scalars(select(Dept))}
    return {
        u: [depts[UUID(x)].name for x in d.path.strip("/").split("/") if UUID(x) in depts]
        for u, d in depts.items()
    }


@router.get("")
async def list_users(
    session: SessionDep,
    settings: SettingsDep,
    status: str | None = None,
    dept_id: UUID | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    p: Principal = require("user:view"),
) -> dict[str, Any]:
    res = await UserService(session, p.actor, settings).list(
        status=status, dept_uuid=dept_id, q=q, page=Page(page, size)
    )
    paths = await _dept_paths(session)
    items = []
    for user, cred, _st in res["rows"]:
        eff = await effective_roles(session, user)
        uc_roles = [e.role.name for e in eff.values() if e.role.is_builtin]
        items.append(
            user_view(
                user,
                cred,
                dept_path=paths.get(user.dept_uuid, []),
                positions=await positions_of(session, user),
                uc_roles=uc_roles,
            )
        )
    return {"total": res["total"], "counts": res["counts"], "items": items}


@router.post("", status_code=201)
async def create_user(
    body: UserCreate,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("user:create"),
) -> dict[str, Any]:
    data = body.model_dump()
    data["dept_uuid"] = data.pop("dept_id")
    user, temp = await UserService(session, p.actor, settings).create(data)
    svc = UserService(session, p.actor, settings)
    return {"user": user_view(user, await svc.credential(user.uuid)), "temp_password": temp}


@router.get("/{user_id}")
async def get_user(
    user_id: UUID, session: SessionDep, settings: SettingsDep, p: Principal = require("user:view")
) -> dict[str, Any]:
    svc = UserService(session, p.actor, settings)
    user = await svc.get(user_id)
    org = OrgService(session, p.actor)
    manager, source = await org.manager_of(user)
    paths = await _dept_paths(session)
    eff = await effective_roles(session, user)
    return user_view(
        user,
        await svc.credential(user.uuid),
        dept_path=paths.get(user.dept_uuid, []),
        positions=await positions_of(session, user),
        uc_roles=[e.role.name for e in eff.values() if e.role.is_builtin],
        manager={**user_brief(manager), "source": source} if manager else None,  # type: ignore[dict-item]
    )


@router.patch("/{user_id}")
async def update_user(
    user_id: UUID,
    body: UserUpdate,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("user:edit"),
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    if "dept_id" in data:
        data["dept_uuid"] = data.pop("dept_id")
    svc = UserService(session, p.actor, settings)
    user, cleared = await svc.update(user_id, data, version=version)
    return {"user": user_view(user, await svc.credential(user.uuid)), "leaders_cleared": cleared}


@router.post("/{user_id}/disable")
async def disable(
    user_id: UUID,
    body: DisableBody,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("user:disable"),
) -> dict[str, Any]:
    svc = UserService(session, p.actor, settings)
    user = await svc.disable(user_id, body.reason)
    return user_view(user, await svc.credential(user.uuid))


@router.post("/{user_id}/enable")
async def enable(
    user_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("user:disable"),
) -> dict[str, Any]:
    svc = UserService(session, p.actor, settings)
    user = await svc.enable(user_id)
    return user_view(user, await svc.credential(user.uuid))


@router.post("/{user_id}/unlock")
async def unlock(
    user_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("user:disable"),
) -> dict[str, Any]:
    svc = UserService(session, p.actor, settings)
    user = await svc.unlock(user_id)
    return user_view(user, await svc.credential(user.uuid))


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    p: Principal = require("user:reset_pwd"),
) -> dict[str, Any]:
    user, temp, expires = await UserService(session, p.actor, settings).reset_password(user_id)
    return {"user_id": str(user.uuid), "temp_password": temp, "temp_expires_at": iso(expires)}


@router.get("/{user_id}/login-logs")
async def login_logs(
    user_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    p: Principal = require("user:view"),
) -> list[dict[str, Any]]:
    logs = await UserService(session, p.actor, settings).login_logs(user_id, Page(page, size))
    return [
        {"time": iso(log.occurred_at), "ip": log.ip, "ok": log.ok, "reason": log.reason}
        for log in logs
    ]


@router.get("/{user_id}/apps")
async def user_apps(
    user_id: UUID, session: SessionDep, settings: SettingsDep, p: Principal = require("user:view")
) -> list[dict[str, Any]]:
    user = await UserService(session, p.actor, settings).get(user_id)
    apps = list(
        await session.scalars(select(App).where(App.is_builtin.is_(False)).order_by(App.id))
    )
    out = []
    for a in apps:
        a_active = a.status == "active"
        if (a_active and await can_access(session, user, a)) or (
            not a_active and (a.scope_all or await _in_scope(session, user, a))
        ):
            out.append(app_view(a))
    return out


async def _in_scope(session: SessionDep, user: Any, app: App) -> bool:
    status, app.status = app.status, "active"
    try:
        return await can_access(session, user, app)
    finally:
        app.status = status


@router.get("/{user_id}/effective")
async def effective(
    user_id: UUID, session: SessionDep, settings: SettingsDep, p: Principal = require("grant:view")
) -> dict[str, Any]:
    """有效权限：角色及来源、各应用的操作码与菜单、数据权限（用户详情「权限」分页，只读）。"""
    user = await UserService(session, p.actor, settings).get(user_id)
    eff = await effective_roles(session, user)
    apps = {
        a.uuid: a
        for a in await session.scalars(select(App).order_by(App.is_builtin.desc(), App.id))
    }
    roles = [
        {
            "id": str(e.role.uuid),
            "name": e.role.name,
            "code": e.role.code,
            "app": apps[e.role.app_uuid].name,
            "sources": [s.label for s in e.sources],
        }
        for e in eff.values()
    ]
    per_app = []
    for a in apps.values():
        snap = await authz_snapshot(session, user, a)
        if a.is_builtin or snap["can_access"]:
            per_app.append({"app": app_view(a), **snap})
    data_svc = DataService(session, p.actor)
    rows = (
        await session.execute(
            select(DataObject, DataType).join(DataType, DataType.uuid == DataObject.data_type_uuid)
        )
    ).all()
    data = []
    for obj, dt in rows:
        level, _hits = await data_svc.level_of(user, obj.uuid)
        if level:
            data.append(
                {
                    "id": str(obj.uuid),
                    "data_code": dt.code,
                    "data_name": obj.data_name or obj.data_id,
                    "level": level_name(level),
                }
            )
    grants = await GrantService(session, p.actor).subject_grants("user", user.uuid)
    return {"roles": roles, "apps": per_app, "data": data, "grants": grants}
