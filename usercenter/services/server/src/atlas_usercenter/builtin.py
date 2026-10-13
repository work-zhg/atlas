"""内置应用（用户中心自身）的操作、菜单与内置角色。

★ 随版本发布：每次 `migrate` 后按编码幂等同步（source = builtin，界面只读），
  而不是写死在某个迁移里 —— 加一个操作码只需改这里。
★ 安全策略、审计日志两个模块本期不做，所以没有它们的操作与菜单（总体设计 §02）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import App, Menu, Operation, Role, RoleMenu, RoleOperation

__all__ = ["SUPER_ROLE_CODE", "UC_APP_NAME", "sync_builtin", "uc_app_uuid"]

UC_APP_NAME = "用户中心"
SUPER_ROLE_CODE = "UC_SUPER"

#: (模块, 操作码, 名称)
OPERATIONS: list[tuple[str, str, str]] = [
    ("组织架构", "org:view", "查看组织架构"),
    ("组织架构", "org:manage", "管理部门（新建 / 编辑 / 删除 / 调整成员）"),
    ("用户", "user:view", "查看用户"),
    ("用户", "user:create", "新建用户"),
    ("用户", "user:edit", "编辑用户"),
    ("用户", "user:disable", "停用 / 启用 / 解锁"),
    ("用户", "user:reset_pwd", "重置密码"),
    ("权限", "grant:view", "查看授权"),
    ("权限", "grant:manage", "授权 / 撤销（岗位、角色 → 用户、组织）"),
    ("权限", "position:view", "查看岗位"),
    ("权限", "position:manage", "管理岗位"),
    ("权限", "role:view", "查看角色"),
    ("权限", "role:manage", "管理角色（新建 / 编辑操作与菜单 / 删除）"),
    ("数据权限", "data:view", "查看全部数据的权限（只读）"),
    ("系统", "app:manage", "应用接入、操作目录、菜单与数据编码"),
]

#: (编码, 名称, 图标, 路由, 公共)
MENUS: list[tuple[str, str, str, str, bool]] = [
    ("org", "组织架构", "🏢", "/org", False),
    ("users", "用户管理", "👤", "/users", False),
    # ★ 公共菜单：人人都可能是某条数据的 Owner，至少要能进「数据授权」
    ("grants", "权限管理", "🛂", "/grants", True),
    ("apps", "应用接入", "🔗", "/apps", False),
]

_ALL_OPS = [code for _, code, _ in OPERATIONS]
#: (编码, 名称, 说明, 操作码, 菜单编码)
ROLES: list[tuple[str, str, str, list[str], list[str]]] = [
    (SUPER_ROLE_CODE, "超级管理员", "用户中心全部操作", _ALL_OPS, ["org", "users", "apps"]),
    (
        "UC_USER_ADMIN",
        "用户管理员",
        "维护组织、用户与岗位，可为用户和组织授权",
        [c for c in _ALL_OPS if c.split(":")[0] in ("org", "user", "grant", "position")]
        + ["role:view"],
        ["org", "users", "apps"],
    ),
    (
        "UC_AUDITOR",
        "审计员",
        "只读：组织、用户、授权与全部数据权限",
        ["org:view", "user:view", "grant:view", "data:view"],
        ["org", "users"],
    ),
]


async def uc_app_uuid(session: AsyncSession) -> UUID:
    app = (await session.execute(select(App).where(App.is_builtin.is_(True)))).scalar_one()
    return app.uuid


async def sync_builtin(session: AsyncSession) -> None:
    app = (await session.execute(select(App).where(App.is_builtin.is_(True)))).scalar_one_or_none()
    if app is None:
        app = App(
            name=UC_APP_NAME,
            icon="👥",
            description="本系统 · 内置应用",
            scope_all=True,
            is_builtin=True,
        )
        session.add(app)
        await session.flush()

    ops = {
        o.code: o
        for o in (await session.scalars(select(Operation).where(Operation.app_uuid == app.uuid)))
    }
    for sort, (module, code, name) in enumerate(OPERATIONS):
        op = ops.pop(code, None)
        if op is None:
            session.add(
                Operation(
                    app_uuid=app.uuid,
                    module=module,
                    code=code,
                    name=name,
                    sort=sort,
                    source="builtin",
                )
            )
        else:
            op.module, op.name, op.sort, op.source = module, name, sort, "builtin"
    for stale in ops.values():
        await session.delete(stale)

    menus = {
        m.code: m for m in (await session.scalars(select(Menu).where(Menu.app_uuid == app.uuid)))
    }
    for sort, (code, name, icon, path, public) in enumerate(MENUS):
        menu = menus.pop(code, None)
        if menu is None:
            session.add(
                Menu(
                    app_uuid=app.uuid,
                    type="menu",
                    code=code,
                    name=name,
                    icon=icon,
                    path=path,
                    is_public=public,
                    sort=sort,
                    source="builtin",
                )
            )
        else:
            menu.name, menu.icon, menu.path, menu.is_public, menu.sort = (
                name,
                icon,
                path,
                public,
                sort,
            )
            menu.source = "builtin"
    for stale in menus.values():
        await session.delete(stale)
    await session.flush()

    op_by_code = {
        o.code: o.uuid
        for o in await session.scalars(select(Operation).where(Operation.app_uuid == app.uuid))
    }
    menu_by_code = {
        m.code: m.uuid for m in await session.scalars(select(Menu).where(Menu.app_uuid == app.uuid))
    }
    for code, name, desc, op_codes, menu_codes in ROLES:
        role = (await session.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
        if role is None:
            role = Role(app_uuid=app.uuid, code=code, name=name, description=desc, is_builtin=True)
            session.add(role)
            await session.flush()
        else:
            role.name, role.description, role.is_builtin = name, desc, True
        # 内置角色的操作与菜单锁定：每次按代码重置
        await session.execute(delete(RoleOperation).where(RoleOperation.role_uuid == role.uuid))
        await session.execute(delete(RoleMenu).where(RoleMenu.role_uuid == role.uuid))
        for c in op_codes:
            session.add(RoleOperation(role_uuid=role.uuid, operation_uuid=op_by_code[c]))
        for c in menu_codes:
            session.add(RoleMenu(role_uuid=role.uuid, menu_uuid=menu_by_code[c]))
    await session.flush()
