"""运营接口 /ops/v1（c4-model/opscenter/code-gateway.html 的 UserCenterOpsApi）：只给运营中心。

    租户目录   列、按 uuid 幂等建（含根部门、首个管理员、开通的应用）、改名字 / 名额 / 到期、
              挡住 / 放开入口、删除（只删已停用的）、补发首个管理员的临时密码、列已过期的
    应用目录   接入应用、凭据、启停；操作码、菜单、数据编码、应用内置角色；给租户开通应用

★ 只认运营中心：每个请求核对签名和时间窗（deps.current_ops）；网关不转发 /ops/*。
★ 看不到租户内容（租户隔离设计 §3.5）：管租户只碰租户本身；管应用目录的请求整个在
  catalog_scope 里 —— 角色只看得到应用内置的，碰人、授权、数据权限这些租户表直接报错。
  影响面只给数字（几条授权、几个岗位），不给租户里的名字。
★ 数据留在用户中心（运营中心不存租户名单和应用目录），运营中心只是界面和编排。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..app.service import AppService
from ..common import iso
from ..db.models import Tenant
from ..db.tenancy import platform_scope, set_catalog
from ..tenant.service import TenantService, tenant_usable
from .apps import IdsBody, RoleCreate, RoleUpdate
from .deps import OpsDep, SessionDep, SettingsDep
from .views import app_view, data_type_view, menu_view, op_view, role_view

router = APIRouter(prefix="/ops/v1", tags=["ops"])


async def _catalog(session: SessionDep, _actor: OpsDep) -> AsyncSession:
    """管应用目录的请求：先验签名，再把会话切到应用目录范围。"""
    set_catalog(session)
    return session


CatalogDep = Annotated[AsyncSession, Depends(_catalog)]


# ───────────────────────────── 请求体


class AdminIn(BaseModel):
    account: str
    name: str
    email: str


class TenantCreate(BaseModel):
    #: 运营中心先生成：超时重做时用同一个 uuid，不会建出两个租户
    uuid: UUID
    code: str
    name: str
    admin: AdminIn
    max_users: int | None = None
    expires_at: datetime | None = None
    #: 开通的应用编码
    apps: list[str] = []


class TenantUpdate(BaseModel):
    name: str | None = None
    max_users: int | None = None
    expires_at: datetime | None = None


class AppSwitch(BaseModel):
    enabled: bool


class AppCreate(BaseModel):
    name: str
    #: 应用编码：内网调用开放接口时 X-Caller-App 写它（如 atlas、teamflow）
    code: str | None = None
    description: str | None = None
    icon: str | None = None
    #: 入口地址：「应用切换」「我的应用」点开去这里
    home_url: str | None = None


class AppUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    description: str | None = None
    icon: str | None = None
    home_url: str | None = None
    version: int | None = None


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


class DataTypeCreate(BaseModel):
    code: str
    name: str
    description: str | None = None


class DataTypeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


# ───────────────────────────── 租户目录


def tenant_view(t: Tenant, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(t.uuid),
        "code": t.code,
        "name": t.name,
        "status": t.status,
        "usable": tenant_usable(t),
        "max_users": t.max_users,
        "expires_at": iso(t.expires_at) if t.expires_at else None,
        "created_at": iso(t.created_at),
        "version": t.version,
        **extra,
    }


@router.get("/tenants")
async def list_tenants(session: SessionDep, actor: OpsDep) -> list[dict[str, Any]]:
    return [tenant_view(t, users=n) for t, n in await TenantService(session, actor).list()]


@router.get("/tenants/expired")
async def expired_tenants(session: SessionDep, actor: OpsDep) -> list[dict[str, Any]]:
    """已过到期时间、但还没停用的租户：运营中心据此发起停用。"""
    return [tenant_view(t) for t in await TenantService(session, actor).expired()]


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: TenantCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    actor: OpsDep,
) -> dict[str, Any]:
    """建租户、根部门与首个租户超级管理员，开通应用。临时密码只在响应里出现。

    已经建过（同一个 uuid 重做）→ 200；首个管理员还没登录过就重发临时密码，登录过为 null。
    """
    tenant, admin, temp, created = await TenantService(session, actor).create(
        uuid=body.uuid,
        code=body.code,
        name=body.name,
        admin=body.admin.model_dump(),
        max_users=body.max_users,
        expires_at=body.expires_at,
        apps=body.apps,
        settings=settings,
    )
    if not created:
        response.status_code = 200
    return {
        "tenant": tenant_view(tenant),
        "admin": {"id": str(admin.uuid), "account": admin.account, "name": admin.name},
        "temp_password": temp,
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: UUID, session: SessionDep, actor: OpsDep) -> dict[str, Any]:
    svc = TenantService(session, actor)
    t = await svc.get(tenant_id)
    return tenant_view(t, users=(await svc.users(t.uuid)).get(t.uuid, 0))


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: UUID, body: TenantUpdate, session: SessionDep, actor: OpsDep
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    return tenant_view(await TenantService(session, actor).update(tenant_id, data))


@router.post("/tenants/{tenant_id}/block")
async def block_tenant(tenant_id: UUID, session: SessionDep, actor: OpsDep) -> dict[str, Any]:
    """挡住入口：登录、网关校验、各系统调开放接口都被拒。在跑的后台工作由各系统挂起。"""
    return tenant_view(await TenantService(session, actor).set_blocked(tenant_id, True))


@router.post("/tenants/{tenant_id}/unblock")
async def unblock_tenant(tenant_id: UUID, session: SessionDep, actor: OpsDep) -> dict[str, Any]:
    return tenant_view(await TenantService(session, actor).set_blocked(tenant_id, False))


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: UUID, session: SessionDep, actor: OpsDep) -> dict[str, Any]:
    """删除租户和它的全部数据。只删已停用的；已经删了也返回成功（幂等）。"""
    deleted = await TenantService(session, actor).delete(tenant_id)
    return {"deleted": deleted, "remaining": False}


@router.post("/tenants/{tenant_id}/admin-password")
async def reissue_admin_password(
    tenant_id: UUID, session: SessionDep, settings: SettingsDep, actor: OpsDep
) -> dict[str, Any]:
    """补发首个管理员的临时密码（旧的作废）。他登录过之后拒绝。"""
    admin, temp = await TenantService(session, actor).reissue_admin_password(tenant_id, settings)
    return {
        "admin": {"id": str(admin.uuid), "account": admin.account, "name": admin.name},
        "temp_password": temp,
    }


@router.get("/tenants/{tenant_id}/apps")
async def tenant_apps(tenant_id: UUID, session: SessionDep, actor: OpsDep) -> list[dict[str, Any]]:
    return [
        {
            "app": {
                "id": str(a.uuid),
                "name": a.name,
                "code": a.code,
                "icon": a.icon,
                "status": a.status,
            },
            "enabled": bool(ta and ta.status == "active"),
            "enabled_at": iso(ta.enabled_at) if ta else None,
        }
        for a, ta in await TenantService(session, actor).apps(tenant_id)
    ]


@router.put("/tenants/{tenant_id}/apps/{app_id}")
async def switch_app(
    tenant_id: UUID, app_id: UUID, body: AppSwitch, session: SessionDep, actor: OpsDep
) -> dict[str, bool]:
    await TenantService(session, actor).set_app(tenant_id, app_id, body.enabled)
    return {"ok": True}


# ───────────────────────────── 应用目录：应用


@router.get("/apps")
async def list_apps(session: CatalogDep, actor: OpsDep) -> list[dict[str, Any]]:
    svc = AppService(session, actor)
    using = await svc.tenants_using()
    return [
        app_view(a, **await svc.counts(a.uuid), tenants=using.get(a.uuid, 0))
        for a in await svc.list()
    ]


@router.post("/apps", status_code=201)
async def create_app(body: AppCreate, session: CatalogDep, actor: OpsDep) -> dict[str, Any]:
    """接入应用。接入后哪个租户都还没开通，见 PUT /tenants/{id}/apps/{app_id}。"""
    app, secret = await AppService(session, actor).create(
        body.name, body.description, body.icon, body.code, body.home_url
    )
    return {"app": app_view(app), "app_secret": secret}


@router.get("/apps/{app_id}")
async def get_app(app_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, Any]:
    svc = AppService(session, actor)
    using = await svc.tenants_using()
    return app_view(await svc.get(app_id), **await svc.counts(app_id), tenants=using.get(app_id, 0))


@router.patch("/apps/{app_id}")
async def update_app(
    app_id: UUID, body: AppUpdate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    return app_view(await AppService(session, actor).update(app_id, data, version))


@router.post("/apps/{app_id}/disable")
async def disable_app(app_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, Any]:
    return app_view(await AppService(session, actor).set_status(app_id, False))


@router.post("/apps/{app_id}/enable")
async def enable_app(app_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, Any]:
    return app_view(await AppService(session, actor).set_status(app_id, True))


@router.post("/apps/{app_id}/rotate-secret")
async def rotate_secret(app_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, Any]:
    app, secret = await AppService(session, actor).rotate_secret(app_id)
    return {"app": app_view(app), "app_secret": secret}


# ───────────────────────────── 应用目录：操作码


@router.get("/apps/{app_id}/operations")
async def list_ops(app_id: UUID, session: CatalogDep, actor: OpsDep) -> list[dict[str, Any]]:
    """roles：绑了这个操作码的应用内置角色（租户自定义的角色看不到）。"""
    svc = AppService(session, actor)
    return [op_view(o, roles=await svc.op_roles(o.uuid)) for o in await svc.operations(app_id)]


@router.post("/apps/{app_id}/operations", status_code=201)
async def create_op(
    app_id: UUID, body: OpCreate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    op = await AppService(session, actor).create_operation(
        app_id, body.module, body.code, body.name
    )
    return op_view(op, roles=[])


@router.patch("/operations/{op_id}")
async def update_op(
    op_id: UUID, body: OpUpdate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    svc = AppService(session, actor)
    return op_view(await svc.update_operation(op_id, body.model_dump(exclude_unset=True)))


@router.delete("/operations/{op_id}")
async def delete_op(
    op_id: UUID, session: CatalogDep, actor: OpsDep, dry_run: bool = False
) -> dict[str, Any]:
    """删除后各租户角色上的这个操作码一并去掉；roles 只列应用内置角色。"""
    return await AppService(session, actor).delete_operation(op_id, dry_run)


# ───────────────────────────── 应用目录：菜单


@router.get("/apps/{app_id}/menus")
async def list_menus(app_id: UUID, session: CatalogDep, actor: OpsDep) -> list[dict[str, Any]]:
    """role_count：绑了这个菜单的角色数（含各租户自定义的，只有数字）。"""
    svc = AppService(session, actor)
    counts = await svc.menu_role_counts(app_id)
    return [menu_view(m, role_count=counts.get(m.uuid, 0)) for m in await svc.menus(app_id)]


@router.post("/apps/{app_id}/menus", status_code=201)
async def create_menu(
    app_id: UUID, body: MenuCreate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    data = body.model_dump()
    data["parent_uuid"] = data.pop("parent_id")
    return menu_view(await AppService(session, actor).create_menu(app_id, data), role_count=0)


@router.patch("/menus/{menu_id}")
async def update_menu(
    menu_id: UUID, body: MenuUpdate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    if "parent_id" in data:
        data["parent_uuid"] = data.pop("parent_id")
    return menu_view(await AppService(session, actor).update_menu(menu_id, data))


@router.post("/menus/{menu_id}/move-up")
async def move_menu_up(menu_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, bool]:
    await AppService(session, actor).move_menu_up(menu_id)
    return {"ok": True}


@router.delete("/menus/{menu_id}")
async def delete_menu(
    menu_id: UUID, session: CatalogDep, actor: OpsDep, dry_run: bool = False
) -> dict[str, Any]:
    return await AppService(session, actor).delete_menu(menu_id, dry_run)


# ───────────────────────────── 应用目录：数据编码


@router.get("/apps/{app_id}/data-types")
async def list_data_types(app_id: UUID, session: CatalogDep, actor: OpsDep) -> list[dict[str, Any]]:
    """data_count：所有租户用这个编码登记的数据条数（只有数字）。"""
    svc = AppService(session, actor)
    out = []
    for d in await svc.data_types(app_id):
        with platform_scope(session):
            n = await svc.data_type_count(d.uuid)
        out.append(data_type_view(d, data_count=n))
    return out


@router.post("/apps/{app_id}/data-types", status_code=201)
async def create_data_type(
    app_id: UUID, body: DataTypeCreate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    dt = await AppService(session, actor).create_data_type(
        app_id, body.code, body.name, body.description
    )
    return data_type_view(dt, data_count=0)


@router.patch("/data-types/{dt_id}")
async def update_data_type(
    dt_id: UUID, body: DataTypeUpdate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    svc = AppService(session, actor)
    return data_type_view(await svc.update_data_type(dt_id, body.model_dump(exclude_unset=True)))


@router.delete("/data-types/{dt_id}")
async def delete_data_type(dt_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, bool]:
    """还有任何租户的数据在用这个编码就拒绝。"""
    await AppService(session, actor).delete_data_type(dt_id)
    return {"ok": True}


# ───────────────────────────── 应用目录：应用内置角色


@router.get("/apps/{app_id}/roles")
async def list_roles(app_id: UUID, session: CatalogDep, actor: OpsDep) -> list[dict[str, Any]]:
    """应用内置角色（所有租户共用）。租户自定义的角色不在这里。"""
    svc = AppService(session, actor)
    return [
        role_view(r, operation_count=len(await svc.role_ops(r.uuid)))
        for r in await svc.roles(app_id)
    ]


@router.post("/apps/{app_id}/roles", status_code=201)
async def create_role(
    app_id: UUID, body: RoleCreate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    role = await AppService(session, actor).create_role(
        app_id, body.name, body.code, body.description, body.copy_from
    )
    return role_view(role)


@router.get("/roles/{role_id}")
async def get_role(role_id: UUID, session: CatalogDep, actor: OpsDep) -> dict[str, Any]:
    svc = AppService(session, actor)
    r = await svc.get_role(role_id)
    return role_view(
        r,
        operation_ids=[str(x) for x in await svc.role_ops(r.uuid)],
        menu_ids=[str(x) for x in await svc.role_menus(r.uuid)],
    )


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: UUID, body: RoleUpdate, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    svc = AppService(session, actor)
    return role_view(await svc.update_role(role_id, body.model_dump(exclude_unset=True)))


@router.put("/roles/{role_id}/operations")
async def set_role_ops(
    role_id: UUID, body: IdsBody, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    return role_view(await AppService(session, actor).set_role_operations(role_id, body.ids))


@router.put("/roles/{role_id}/menus")
async def set_role_menus(
    role_id: UUID, body: IdsBody, session: CatalogDep, actor: OpsDep
) -> dict[str, Any]:
    return role_view(await AppService(session, actor).set_role_menus(role_id, body.ids))


@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: UUID, session: CatalogDep, actor: OpsDep, dry_run: bool = False
) -> dict[str, Any]:
    """各租户里这个角色的授权一并删除；影响面只给数字（几条授权、几个岗位）。"""
    return await AppService(session, actor).delete_role(role_id, dry_run)
