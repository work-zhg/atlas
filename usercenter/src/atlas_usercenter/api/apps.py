"""应用接入接口：应用、凭据、可访问范围、操作、菜单、角色、数据编码。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from ..app.service import AppService
from ..db.models import User
from ..errors import Forbidden
from ..perm.effective import can_access, effective_roles, role_holders, visible_menu_tree
from .deps import Principal, PrincipalDep, SessionDep, SettingsDep, require
from .views import app_view, data_type_view, menu_view, op_view, role_view

router = APIRouter(prefix="/api/v1", tags=["apps"])


def _can_view_meta(p: Principal) -> None:
    """操作目录、菜单、角色：app:manage 或 role:view 可看（维护角色需要看到它们）。"""
    if not (p.has("app:manage") or p.has("role:view")):
        raise Forbidden("PERMISSION_DENIED", "缺少操作权限：app:manage 或 role:view")


class AppCreate(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = None


class AppUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    version: int | None = None


class ScopeBody(BaseModel):
    all: bool
    dept_ids: list[UUID] = []


class OpCreate(BaseModel):
    module: str
    code: str
    name: str


class OpUpdate(BaseModel):
    module: str | None = None
    name: str | None = None


class MenuCreate(BaseModel):
    type: str
    code: str
    name: str
    icon: str | None = None
    path: str | None = None
    is_public: bool = False
    parent_id: UUID | None = None


class MenuUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None
    path: str | None = None
    is_public: bool | None = None
    parent_id: UUID | None = None


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


class DataTypeCreate(BaseModel):
    code: str
    name: str
    description: str | None = None
    admin_operation_code: str | None = None


class DataTypeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    admin_operation_code: str | None = None


# ───────────────────────────── 应用


@router.get("/apps")
async def list_apps(session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    out = []
    for a in await svc.list():
        extra: dict[str, Any] = await svc.counts(a.uuid)
        if p.has("app:manage"):
            extra["accessible_users"] = await svc.accessible_users(a)
            extra["scope_dept_ids"] = [str(d) for d in await svc.scope_depts(a.uuid)]
        out.append(
            app_view(a, **extra)
            if p.has("app:manage")
            else {**app_view(a, **extra), "app_key": None, "secret_tail": None}
        )
    return out


@router.post("/apps", status_code=201)
async def create_app(
    body: AppCreate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    app, secret = await AppService(session, p.actor).create(body.name, body.description, body.icon)
    return {"app": app_view(app), "app_secret": secret}


@router.get("/apps/{app_id}")
async def get_app(app_id: UUID, session: SessionDep, p: PrincipalDep) -> dict[str, Any]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    app = await svc.get(app_id)
    view = app_view(app, **await svc.counts(app.uuid))
    if p.has("app:manage"):
        view["accessible_users"] = await svc.accessible_users(app)
        view["scope_dept_ids"] = [str(d) for d in await svc.scope_depts(app.uuid)]
    else:
        view["app_key"] = view["secret_tail"] = None
    return view


@router.patch("/apps/{app_id}")
async def update_app(
    app_id: UUID, body: AppUpdate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    return app_view(await AppService(session, p.actor).update(app_id, data, version))


@router.post("/apps/{app_id}/disable")
async def disable_app(
    app_id: UUID, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return app_view(await AppService(session, p.actor).set_status(app_id, False))


@router.post("/apps/{app_id}/enable")
async def enable_app(
    app_id: UUID, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return app_view(await AppService(session, p.actor).set_status(app_id, True))


@router.post("/apps/{app_id}/rotate-secret")
async def rotate_secret(
    app_id: UUID, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    app, secret = await AppService(session, p.actor).rotate_secret(app_id)
    return {"app": app_view(app), "app_secret": secret}


@router.put("/apps/{app_id}/scope")
async def set_scope(
    app_id: UUID, body: ScopeBody, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    n = await AppService(session, p.actor).set_scope(app_id, body.all, body.dept_ids)
    return {"accessible_users": n}


# ───────────────────────────── 操作


@router.get("/apps/{app_id}/operations")
async def list_ops(app_id: UUID, session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    return [op_view(o, roles=await svc.op_roles(o.uuid)) for o in await svc.operations(app_id)]


@router.post("/apps/{app_id}/operations", status_code=201)
async def create_op(
    app_id: UUID, body: OpCreate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return op_view(
        await AppService(session, p.actor).create_operation(
            app_id, body.module, body.code, body.name
        ),
        roles=[],
    )


@router.patch("/operations/{op_id}")
async def update_op(
    op_id: UUID, body: OpUpdate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return op_view(
        await AppService(session, p.actor).update_operation(
            op_id, body.model_dump(exclude_unset=True)
        )
    )


@router.delete("/operations/{op_id}")
async def delete_op(
    op_id: UUID, session: SessionDep, dry_run: bool = False, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return await AppService(session, p.actor).delete_operation(op_id, dry_run)


# ───────────────────────────── 菜单


@router.get("/apps/{app_id}/menus")
async def list_menus(app_id: UUID, session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    _can_view_meta(p)
    svc = AppService(session, p.actor)
    counts = await svc.menu_role_counts(app_id)
    return [menu_view(m, role_count=counts.get(m.uuid, 0)) for m in await svc.menus(app_id)]


@router.post("/apps/{app_id}/menus", status_code=201)
async def create_menu(
    app_id: UUID, body: MenuCreate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    data = body.model_dump()
    data["parent_uuid"] = data.pop("parent_id")
    return menu_view(await AppService(session, p.actor).create_menu(app_id, data), role_count=0)


@router.patch("/menus/{menu_id}")
async def update_menu(
    menu_id: UUID, body: MenuUpdate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    if "parent_id" in data:
        data["parent_uuid"] = data.pop("parent_id")
    return menu_view(await AppService(session, p.actor).update_menu(menu_id, data))


@router.post("/menus/{menu_id}/move-up")
async def move_menu_up(
    menu_id: UUID, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, bool]:
    await AppService(session, p.actor).move_menu_up(menu_id)
    return {"ok": True}


@router.delete("/menus/{menu_id}")
async def delete_menu(
    menu_id: UUID, session: SessionDep, dry_run: bool = False, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return await AppService(session, p.actor).delete_menu(menu_id, dry_run)


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


@router.post("/apps/{app_id}/data-types", status_code=201)
async def create_data_type(
    app_id: UUID, body: DataTypeCreate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return data_type_view(
        await AppService(session, p.actor).create_data_type(
            app_id, body.code, body.name, body.description, body.admin_operation_code
        ),
        data_count=0,
    )


@router.patch("/data-types/{dt_id}")
async def update_data_type(
    dt_id: UUID, body: DataTypeUpdate, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, Any]:
    return data_type_view(
        await AppService(session, p.actor).update_data_type(
            dt_id, body.model_dump(exclude_unset=True)
        )
    )


@router.delete("/data-types/{dt_id}")
async def delete_data_type(
    dt_id: UUID, session: SessionDep, p: Principal = require("app:manage")
) -> dict[str, bool]:
    await AppService(session, p.actor).delete_data_type(dt_id)
    return {"ok": True}


_ = SettingsDep
