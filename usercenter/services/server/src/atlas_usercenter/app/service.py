"""应用接入（应用接入设计）：应用、凭据、可访问范围、操作、菜单、角色、数据编码、接口令牌。

★ 编码一律不可改（操作码 / 菜单编码 / 角色编码 / 数据编码都写在应用代码里）。
★ 内置应用（用户中心）：不能停用；操作与菜单只读；内置角色锁定。
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..common import require_text
from ..db.models import (
    App,
    AppScopeDept,
    AppToken,
    DataObject,
    DataType,
    Dept,
    Grant,
    Menu,
    Operation,
    PositionRole,
    Role,
    RoleMenu,
    RoleOperation,
    User,
)
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound, Unauthorized
from ..security import hash_token, new_app_key, new_secret, new_token, token_equals
from ..settings import UCSettings

__all__ = ["AppService"]

OP_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")
MENU_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
MENU_PATH_RE = re.compile(r"^/[\w\-/]*$")
ROLE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
DATA_CODE_RE = re.compile(r"^[A-Z][A-Za-z0-9]{1,31}$")
MENU_MAX_DEPTH = 3


class AppService:
    def __init__(
        self, session: AsyncSession, actor: Actor, settings: UCSettings | None = None
    ) -> None:
        self.session = session
        self.actor = actor
        self.settings = settings

    # ───────────────────────────── 应用

    async def get(self, uuid: UUID) -> App:
        app = (await self.session.execute(select(App).where(App.uuid == uuid))).scalar_one_or_none()
        if app is None:
            raise NotFound("APP_NOT_FOUND", "应用不存在")
        return app

    async def list(self) -> list[App]:
        return list(await self.session.scalars(select(App).order_by(App.is_builtin.desc(), App.id)))

    async def counts(self, app_uuid: UUID) -> dict[str, int]:
        async def n(model: Any, *where: Any) -> int:
            return int(
                await self.session.scalar(select(func.count()).select_from(model).where(*where))
                or 0
            )

        return {
            "operations": await n(Operation, Operation.app_uuid == app_uuid),
            "menus": await n(Menu, Menu.app_uuid == app_uuid, Menu.type == "menu"),
            "roles": await n(Role, Role.app_uuid == app_uuid),
            "data_types": await n(DataType, DataType.app_uuid == app_uuid),
        }

    def _mutable(self, app: App) -> None:
        if app.is_builtin:
            raise Conflict("BUILTIN_READONLY", "内置应用的配置、操作与菜单不能修改")

    async def create(self, name: str, description: str | None, icon: str | None) -> tuple[App, str]:
        name = require_text(name, "name", "应用名称", 50)
        if await self.session.scalar(select(App.uuid).where(App.name == name)):
            raise Conflict("APP_NAME_EXISTS", "应用名称已存在")
        secret = new_secret()
        app = App(
            name=name,
            description=(description or "").strip()[:200] or None,
            icon=(icon or "").strip()[:16] or "🧱",
            app_key=new_app_key(),
            secret_hash=hash_token(secret),
            secret_tail=secret[-4:],
            secret_rotated_at=utcnow(),
            scope_all=False,  # ★ 默认无人可访问，避免配置没做完就对全员开放
            created_by=self.actor.user_uuid,
            updated_by=self.actor.user_uuid,
        )
        self.session.add(app)
        await self.session.flush()
        record(self.session, self.actor, "app.create", "app", app.uuid, name)
        return app, secret

    async def update(self, uuid: UUID, data: dict[str, Any], version: int | None = None) -> App:
        app = await self.get(uuid)
        self._mutable(app)
        if version is not None and version != app.version:
            raise Conflict("VERSION_CONFLICT", "该应用已被他人修改，请刷新后重试")
        changes = {}
        if "name" in data:
            name = require_text(data["name"], "name", "应用名称", 50)
            if name != app.name:
                if await self.session.scalar(
                    select(App.uuid).where(App.name == name, App.uuid != app.uuid)
                ):
                    raise Conflict("APP_NAME_EXISTS", "应用名称已存在")
                changes["name"] = [app.name, name]
                app.name = name
        for key, limit in (("description", 200), ("icon", 16)):
            if key in data:
                val = (data[key] or "").strip()[:limit] or None
                if val != getattr(app, key):
                    changes[key] = [getattr(app, key), val]
                    setattr(app, key, val)
        if changes:
            app.version += 1
            app.updated_by = self.actor.user_uuid
            record(
                self.session, self.actor, "app.update", "app", app.uuid, app.name, changes=changes
            )
        await self.session.flush()
        return app

    async def set_status(self, uuid: UUID, active: bool) -> App:
        app = await self.get(uuid)
        if app.is_builtin:
            raise Conflict("BUILTIN_READONLY", "内置应用不能停用")
        status = "active" if active else "disabled"
        if app.status != status:
            app.status = status
            app.version += 1
            if not active:
                # 已签发的接口令牌立即失效
                await self.session.execute(
                    AppToken.__table__.delete().where(AppToken.app_uuid == app.uuid)
                )
            record(
                self.session,
                self.actor,
                "app.enable" if active else "app.disable",
                "app",
                app.uuid,
                app.name,
            )
        await self.session.flush()
        return app

    async def rotate_secret(self, uuid: UUID) -> tuple[App, str]:
        app = await self.get(uuid)
        self._mutable(app)
        secret = new_secret()
        app.secret_hash = hash_token(secret)
        app.secret_tail = secret[-4:]
        app.secret_rotated_at = utcnow()
        app.version += 1
        await self.session.execute(AppToken.__table__.delete().where(AppToken.app_uuid == app.uuid))
        record(
            self.session,
            self.actor,
            "app.rotate_secret",
            "app",
            app.uuid,
            app.name,
            tail=app.secret_tail,
        )
        await self.session.flush()
        return app, secret

    async def scope_depts(self, app_uuid: UUID) -> list[UUID]:
        return list(
            await self.session.scalars(
                select(AppScopeDept.dept_uuid).where(AppScopeDept.app_uuid == app_uuid)
            )
        )

    async def accessible_users(self, app: App) -> int:
        stmt = select(func.count()).select_from(User).where(User.status == "active")
        if not (app.is_builtin or app.scope_all):
            depts = list(
                await self.session.scalars(
                    select(Dept)
                    .join(AppScopeDept, AppScopeDept.dept_uuid == Dept.uuid)
                    .where(AppScopeDept.app_uuid == app.uuid)
                )
            )
            if not depts:
                return 0
            from sqlalchemy import or_

            stmt = stmt.join(Dept, Dept.uuid == User.dept_uuid).where(
                or_(*[Dept.path.like(d.path + "%") for d in depts])
            )
        return int(await self.session.scalar(stmt) or 0)

    async def set_scope(self, uuid: UUID, scope_all: bool, dept_uuids: list[UUID]) -> int:
        app = await self.get(uuid)
        self._mutable(app)
        if not scope_all and dept_uuids:
            found = set(
                await self.session.scalars(select(Dept.uuid).where(Dept.uuid.in_(dept_uuids)))
            )
            if len(found) != len(set(dept_uuids)):
                raise Invalid("DEPT_NOT_FOUND", "有部门不存在")
        before = await self.accessible_users(app)
        app.scope_all = scope_all
        await self.session.execute(
            AppScopeDept.__table__.delete().where(AppScopeDept.app_uuid == app.uuid)
        )
        if not scope_all:
            for d in set(dept_uuids):
                self.session.add(AppScopeDept(app_uuid=app.uuid, dept_uuid=d))
        app.version += 1
        await self.session.flush()
        after = await self.accessible_users(app)
        record(
            self.session,
            self.actor,
            "app.scope",
            "app",
            app.uuid,
            app.name,
            scope_all=scope_all,
            depts=list(set(dept_uuids)),
            users_before=before,
            users_after=after,
        )
        return after

    # ───────────────────────────── 接口令牌（开放接口）

    async def issue_token(self, app_key: str, secret: str) -> tuple[str, int]:
        app = (
            await self.session.execute(select(App).where(App.app_key == (app_key or "")))
        ).scalar_one_or_none()
        if app is None or not app.secret_hash or not token_equals(secret or "", app.secret_hash):
            raise Unauthorized("INVALID_APP", "App Key 或 App Secret 错误")
        if app.status != "active":
            raise Unauthorized("APP_DISABLED", "应用已停用")
        assert self.settings is not None
        token = new_token()
        ttl = self.settings.app_token_hours * 3600
        self.session.add(
            AppToken(
                token_hash=hash_token(token),
                app_uuid=app.uuid,
                expires_at=utcnow() + timedelta(seconds=ttl),
            )
        )
        # 顺手清掉该应用过期的令牌
        await self.session.execute(
            AppToken.__table__.delete().where(
                AppToken.app_uuid == app.uuid, AppToken.expires_at < utcnow()
            )
        )
        await self.session.flush()
        return token, ttl

    async def resolve_token(self, token: str | None) -> App:
        if not token:
            raise Unauthorized("INVALID_TOKEN", "缺少接口令牌")
        row = (
            await self.session.execute(
                select(AppToken).where(AppToken.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if row is None or row.expires_at < utcnow():
            raise Unauthorized("INVALID_TOKEN", "接口令牌无效或已过期")
        app = await self.get(row.app_uuid)
        if app.status != "active":
            raise Unauthorized("APP_DISABLED", "应用已停用")
        return app

    # ───────────────────────────── 操作

    async def operations(self, app_uuid: UUID) -> list[Operation]:
        return list(
            await self.session.scalars(
                select(Operation)
                .where(Operation.app_uuid == app_uuid)
                .order_by(Operation.sort, Operation.id)
            )
        )

    async def op_roles(self, op_uuid: UUID) -> list[str]:
        return list(
            await self.session.scalars(
                select(Role.name)
                .join(RoleOperation, RoleOperation.role_uuid == Role.uuid)
                .where(RoleOperation.operation_uuid == op_uuid)
            )
        )

    async def _op(self, uuid: UUID) -> Operation:
        op = (
            await self.session.execute(select(Operation).where(Operation.uuid == uuid))
        ).scalar_one_or_none()
        if op is None:
            raise NotFound("OPERATION_NOT_FOUND", "操作不存在")
        return op

    async def create_operation(
        self, app_uuid: UUID, module: str, code: str, name: str
    ) -> Operation:
        app = await self.get(app_uuid)
        self._mutable(app)
        module = require_text(module, "module", "模块", 32)
        name = require_text(name, "name", "操作名称", 64)
        code = (code or "").strip()
        if not OP_CODE_RE.match(code) or len(code) > 64:
            raise Invalid(
                "OP_CODE_INVALID", "操作码格式：资源:动作（小写字母、数字、下划线）", field="code"
            )
        if await self.session.scalar(
            select(Operation.uuid).where(Operation.app_uuid == app.uuid, Operation.code == code)
        ):
            raise Conflict("OP_CODE_EXISTS", "该应用已有相同操作码")
        sort = await self.session.scalar(
            select(func.coalesce(func.max(Operation.sort), 0) + 1).where(
                Operation.app_uuid == app.uuid
            )
        )
        op = Operation(app_uuid=app.uuid, module=module, code=code, name=name, sort=sort)
        self.session.add(op)
        await self.session.flush()
        record(
            self.session,
            self.actor,
            "operation.create",
            "operation",
            op.uuid,
            f"{app.name} · {code}",
        )
        return op

    async def update_operation(self, uuid: UUID, data: dict[str, Any]) -> Operation:
        op = await self._op(uuid)
        self._mutable(await self.get(op.app_uuid))
        if "module" in data:
            op.module = require_text(data["module"], "module", "模块", 32)
        if "name" in data:
            op.name = require_text(data["name"], "name", "操作名称", 64)
        record(self.session, self.actor, "operation.update", "operation", op.uuid, op.code)
        await self.session.flush()
        return op

    async def delete_operation(self, uuid: UUID, dry_run: bool = False) -> dict[str, Any]:
        op = await self._op(uuid)
        app = await self.get(op.app_uuid)
        self._mutable(app)
        roles = await self.op_roles(op.uuid)
        if not dry_run:
            await self.session.delete(op)
            await self.session.flush()
            record(
                self.session,
                self.actor,
                "operation.delete",
                "operation",
                op.uuid,
                f"{app.name} · {op.code}",
                roles=roles,
            )
        return {"roles": roles}

    # ───────────────────────────── 菜单

    async def menus(self, app_uuid: UUID) -> list[Menu]:
        return list(
            await self.session.scalars(
                select(Menu).where(Menu.app_uuid == app_uuid).order_by(Menu.sort, Menu.id)
            )
        )

    async def menu_role_counts(self, app_uuid: UUID) -> dict[UUID, int]:
        rows = await self.session.execute(
            select(RoleMenu.menu_uuid, func.count())
            .join(Menu, Menu.uuid == RoleMenu.menu_uuid)
            .where(Menu.app_uuid == app_uuid)
            .group_by(RoleMenu.menu_uuid)
        )
        return dict(rows.all())

    async def _menu(self, uuid: UUID) -> Menu:
        m = (await self.session.execute(select(Menu).where(Menu.uuid == uuid))).scalar_one_or_none()
        if m is None:
            raise NotFound("MENU_NOT_FOUND", "菜单不存在")
        return m

    async def _menu_depth(self, parent_uuid: UUID | None) -> int:
        depth = 0
        while parent_uuid is not None:
            depth += 1
            parent_uuid = await self.session.scalar(
                select(Menu.parent_uuid).where(Menu.uuid == parent_uuid)
            )
        return depth

    async def _check_menu_parent(
        self, app_uuid: UUID, parent_uuid: UUID | None, self_uuid: UUID | None = None
    ) -> None:
        if parent_uuid is None:
            return
        parent = await self._menu(parent_uuid)
        if parent.app_uuid != app_uuid or parent.type != "dir":
            raise Invalid("MENU_PARENT_INVALID", "上级只能是本应用的目录")
        # 防环：上级不能是自己或自己的下级
        cur: UUID | None = parent_uuid
        while cur is not None:
            if cur == self_uuid:
                raise Invalid("MENU_PARENT_INVALID", "上级不能是自己或自己的下级")
            cur = await self.session.scalar(select(Menu.parent_uuid).where(Menu.uuid == cur))
        if await self._menu_depth(parent_uuid) + 1 > MENU_MAX_DEPTH:
            raise Invalid("MENU_DEPTH_EXCEEDED", f"菜单最多 {MENU_MAX_DEPTH} 级")

    def _check_menu_fields(self, type_: str, data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "name" in data:
            out["name"] = require_text(data["name"], "name", "名称", 32)
        if "icon" in data:
            out["icon"] = (data["icon"] or "").strip()[:16] or None
        if type_ == "menu":
            if "path" in data:
                path = (data["path"] or "").strip()
                if not MENU_PATH_RE.match(path) or len(path) > 200:
                    raise Invalid("VALIDATION_FAILED", "路由需以 / 开头", field="path")
                out["path"] = path
            if "is_public" in data:
                out["is_public"] = bool(data["is_public"])
        return out

    async def _check_menu_path(
        self, app_uuid: UUID, path: str | None, exclude: UUID | None = None
    ) -> None:
        if path is None:
            return
        stmt = select(Menu.uuid).where(Menu.app_uuid == app_uuid, Menu.path == path)
        if exclude:
            stmt = stmt.where(Menu.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("MENU_PATH_EXISTS", "路由已被其他菜单使用")

    async def create_menu(self, app_uuid: UUID, data: dict[str, Any]) -> Menu:
        app = await self.get(app_uuid)
        self._mutable(app)
        type_ = data.get("type")
        if type_ not in ("dir", "menu"):
            raise Invalid("VALIDATION_FAILED", "类型只能是目录或菜单", field="type")
        code = (data.get("code") or "").strip()
        if not MENU_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED", "编码格式：小写字母开头，小写字母、数字、下划线", field="code"
            )
        if await self.session.scalar(
            select(Menu.uuid).where(Menu.app_uuid == app.uuid, Menu.code == code)
        ):
            raise Conflict("MENU_CODE_EXISTS", "菜单编码已存在")
        fields = self._check_menu_fields(
            type_,
            {
                "name": data.get("name"),
                "icon": data.get("icon"),
                "path": data.get("path"),
                "is_public": data.get("is_public"),
            }
            if type_ == "menu"
            else {"name": data.get("name"), "icon": data.get("icon")},
        )
        parent_uuid = data.get("parent_uuid")
        await self._check_menu_parent(app.uuid, parent_uuid)
        await self._check_menu_path(app.uuid, fields.get("path"))
        sort = await self.session.scalar(
            select(func.coalesce(func.max(Menu.sort), 0) + 1).where(
                Menu.app_uuid == app.uuid, Menu.parent_uuid == parent_uuid
            )
        )
        menu = Menu(
            app_uuid=app.uuid, parent_uuid=parent_uuid, type=type_, code=code, sort=sort, **fields
        )
        self.session.add(menu)
        await self.session.flush()
        record(
            self.session,
            self.actor,
            "menu.create",
            "menu",
            menu.uuid,
            f"{app.name} · {menu.name}",
            code=code,
            path=menu.path,
        )
        return menu

    async def update_menu(self, uuid: UUID, data: dict[str, Any]) -> Menu:
        menu = await self._menu(uuid)
        self._mutable(await self.get(menu.app_uuid))
        fields = self._check_menu_fields(menu.type, data)
        if "path" in fields:
            await self._check_menu_path(menu.app_uuid, fields["path"], exclude=menu.uuid)
        if "parent_uuid" in data and data["parent_uuid"] != menu.parent_uuid:
            await self._check_menu_parent(menu.app_uuid, data["parent_uuid"], self_uuid=menu.uuid)
            menu.parent_uuid = data["parent_uuid"]
        for k, v in fields.items():
            setattr(menu, k, v)
        record(self.session, self.actor, "menu.update", "menu", menu.uuid, menu.name)
        await self.session.flush()
        return menu

    async def move_menu_up(self, uuid: UUID) -> None:
        menu = await self._menu(uuid)
        self._mutable(await self.get(menu.app_uuid))
        siblings = list(
            await self.session.scalars(
                select(Menu)
                .where(Menu.app_uuid == menu.app_uuid, Menu.parent_uuid == menu.parent_uuid)
                .order_by(Menu.sort, Menu.id)
            )
        )
        i = siblings.index(menu)
        if i > 0:
            # 重新编号再交换，避免 sort 重复时交换无效
            for n, m in enumerate(siblings):
                m.sort = n
            siblings[i - 1].sort, menu.sort = menu.sort, siblings[i - 1].sort
        await self.session.flush()

    async def delete_menu(self, uuid: UUID, dry_run: bool = False) -> dict[str, Any]:
        menu = await self._menu(uuid)
        app = await self.get(menu.app_uuid)
        self._mutable(app)
        if await self.session.scalar(
            select(Menu.uuid).where(Menu.parent_uuid == menu.uuid).limit(1)
        ):
            raise Conflict("MENU_HAS_CHILDREN", "目录下还有菜单，请先删除或移走")
        roles = list(
            await self.session.scalars(
                select(Role.name)
                .join(RoleMenu, RoleMenu.role_uuid == Role.uuid)
                .where(RoleMenu.menu_uuid == menu.uuid)
            )
        )
        if not dry_run:
            await self.session.delete(menu)
            await self.session.flush()
            record(
                self.session,
                self.actor,
                "menu.delete",
                "menu",
                menu.uuid,
                f"{app.name} · {menu.name}",
                roles=roles,
            )
        return {"roles": roles}

    # ───────────────────────────── 角色

    async def roles(self, app_uuid: UUID) -> list[Role]:
        return list(
            await self.session.scalars(
                select(Role)
                .where(Role.app_uuid == app_uuid)
                .order_by(Role.is_builtin.desc(), Role.id)
            )
        )

    async def get_role(self, uuid: UUID) -> Role:
        role = (
            await self.session.execute(select(Role).where(Role.uuid == uuid))
        ).scalar_one_or_none()
        if role is None:
            raise NotFound("ROLE_NOT_FOUND", "角色不存在")
        return role

    async def role_ops(self, role_uuid: UUID) -> list[UUID]:
        return list(
            await self.session.scalars(
                select(RoleOperation.operation_uuid).where(RoleOperation.role_uuid == role_uuid)
            )
        )

    async def role_menus(self, role_uuid: UUID) -> list[UUID]:
        return list(
            await self.session.scalars(
                select(RoleMenu.menu_uuid).where(RoleMenu.role_uuid == role_uuid)
            )
        )

    def _role_mutable(self, role: Role) -> None:
        if role.is_builtin:
            raise Conflict("BUILTIN_READONLY", "内置角色不能修改")

    async def create_role(
        self,
        app_uuid: UUID,
        name: str,
        code: str,
        description: str | None,
        copy_from: UUID | None = None,
    ) -> Role:
        app = await self.get(app_uuid)
        name = require_text(name, "name", "角色名称", 32)
        code = (code or "").strip()
        if not ROLE_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED",
                "编码格式：大写字母开头，大写字母、数字、下划线，2–32 位",
                field="code",
            )
        if await self.session.scalar(select(Role.uuid).where(Role.code == code)):
            raise Conflict("ROLE_CODE_EXISTS", "角色编码已存在")
        if await self.session.scalar(
            select(Role.uuid).where(Role.app_uuid == app.uuid, Role.name == name)
        ):
            raise Conflict("ROLE_NAME_EXISTS", "同一应用下已有同名角色")
        src = await self.get_role(copy_from) if copy_from else None
        if src and src.app_uuid != app.uuid:
            raise Invalid("CROSS_APP_REFERENCE", "只能复制本应用的角色")
        role = Role(
            app_uuid=app.uuid,
            code=code,
            name=name,
            description=(description or "").strip()[:200] or None,
            created_by=self.actor.user_uuid,
            updated_by=self.actor.user_uuid,
        )
        self.session.add(role)
        await self.session.flush()
        if src:
            for o in await self.role_ops(src.uuid):
                self.session.add(RoleOperation(role_uuid=role.uuid, operation_uuid=o))
            for m in await self.role_menus(src.uuid):
                self.session.add(RoleMenu(role_uuid=role.uuid, menu_uuid=m))
        record(
            self.session,
            self.actor,
            "role.copy" if src else "role.create",
            "role",
            role.uuid,
            f"{app.name} · {name}",
            code=code,
            copy_from=src.name if src else None,
        )
        await self.session.flush()
        return role

    async def update_role(self, uuid: UUID, data: dict[str, Any]) -> Role:
        role = await self.get_role(uuid)
        self._role_mutable(role)
        if "name" in data:
            name = require_text(data["name"], "name", "角色名称", 32)
            if await self.session.scalar(
                select(Role.uuid).where(
                    Role.app_uuid == role.app_uuid, Role.name == name, Role.uuid != role.uuid
                )
            ):
                raise Conflict("ROLE_NAME_EXISTS", "同一应用下已有同名角色")
            role.name = name
        if "description" in data:
            role.description = (data["description"] or "").strip()[:200] or None
        role.version += 1
        role.updated_by = self.actor.user_uuid
        record(self.session, self.actor, "role.update", "role", role.uuid, role.name)
        await self.session.flush()
        return role

    async def set_role_operations(self, uuid: UUID, op_uuids: list[UUID]) -> Role:
        role = await self.get_role(uuid)
        self._role_mutable(role)
        ops = list(
            await self.session.scalars(select(Operation).where(Operation.uuid.in_(op_uuids)))
        )
        if len(ops) != len(set(op_uuids)) or any(o.app_uuid != role.app_uuid for o in ops):
            raise Invalid("CROSS_APP_REFERENCE", "只能勾选本应用的操作")
        before = set(await self.role_ops(role.uuid))
        after = {o.uuid for o in ops}
        await self.session.execute(
            RoleOperation.__table__.delete().where(RoleOperation.role_uuid == role.uuid)
        )
        for o in after:
            self.session.add(RoleOperation(role_uuid=role.uuid, operation_uuid=o))
        role.version += 1
        record(
            self.session,
            self.actor,
            "role.operations",
            "role",
            role.uuid,
            role.name,
            added=[o.code for o in ops if o.uuid not in before],
            removed=len(before - after),
        )
        await self.session.flush()
        return role

    async def set_role_menus(self, uuid: UUID, menu_uuids: list[UUID]) -> Role:
        role = await self.get_role(uuid)
        self._role_mutable(role)
        menus = list(await self.session.scalars(select(Menu).where(Menu.uuid.in_(menu_uuids))))
        if len(menus) != len(set(menu_uuids)) or any(m.app_uuid != role.app_uuid for m in menus):
            raise Invalid("CROSS_APP_REFERENCE", "只能勾选本应用的菜单")
        # 只存菜单；目录可见性由子菜单推导；公共菜单无需绑定
        keep = {m.uuid for m in menus if m.type == "menu" and not m.is_public}
        await self.session.execute(
            RoleMenu.__table__.delete().where(RoleMenu.role_uuid == role.uuid)
        )
        for m in keep:
            self.session.add(RoleMenu(role_uuid=role.uuid, menu_uuid=m))
        role.version += 1
        record(
            self.session, self.actor, "role.menus", "role", role.uuid, role.name, menus=len(keep)
        )
        await self.session.flush()
        return role

    async def delete_role(self, uuid: UUID, dry_run: bool = False) -> dict[str, Any]:
        from ..perm.effective import role_holders

        role = await self.get_role(uuid)
        self._role_mutable(role)
        grants = int(
            await self.session.scalar(
                select(func.count()).select_from(Grant).where(Grant.role_uuid == role.uuid)
            )
            or 0
        )
        from ..db.models import Position

        positions = list(
            await self.session.scalars(
                select(Position.name)
                .join(PositionRole, PositionRole.position_uuid == Position.uuid)
                .where(PositionRole.role_uuid == role.uuid)
            )
        )
        holders = await role_holders(self.session, role.uuid)
        impact = {"grants": grants, "positions": positions, "holders": len(holders)}
        if not dry_run:
            # 授权与岗位默认角色由外键 ON DELETE CASCADE 一并清理
            await self.session.delete(role)
            await self.session.flush()
            record(self.session, self.actor, "role.delete", "role", role.uuid, role.name, **impact)
        return impact

    # ───────────────────────────── 数据编码

    async def data_types(self, app_uuid: UUID | None = None) -> list[DataType]:
        stmt = select(DataType).order_by(DataType.id)
        if app_uuid:
            stmt = stmt.where(DataType.app_uuid == app_uuid)
        return list(await self.session.scalars(stmt))

    async def data_type_count(self, dt_uuid: UUID) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(DataObject)
                .where(DataObject.data_type_uuid == dt_uuid)
            )
            or 0
        )

    async def _check_admin_op(self, app_uuid: UUID, code: str | None) -> str | None:
        """数据管理员操作码必须是本应用操作目录里的操作。"""
        code = (code or "").strip() or None
        if code and not await self.session.scalar(
            select(Operation.uuid).where(Operation.app_uuid == app_uuid, Operation.code == code)
        ):
            raise Invalid(
                "VALIDATION_FAILED",
                "数据管理员操作码必须是本应用操作目录中的操作",
                field="admin_operation_code",
            )
        return code

    async def create_data_type(
        self,
        app_uuid: UUID,
        code: str,
        name: str,
        description: str | None,
        admin_operation_code: str | None = None,
    ) -> DataType:
        app = await self.get(app_uuid)
        self._mutable(app)
        code = (code or "").strip()
        if not DATA_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED", "数据编码格式：大写字母开头，字母和数字，2–32 位", field="code"
            )
        if await self.session.scalar(select(DataType.uuid).where(DataType.code == code)):
            raise Conflict("DATA_TYPE_EXISTS", "数据编码已存在")
        dt = DataType(
            app_uuid=app.uuid,
            code=code,
            name=require_text(name, "name", "名称", 32),
            description=(description or "").strip()[:200] or None,
            admin_operation_code=await self._check_admin_op(app.uuid, admin_operation_code),
        )
        self.session.add(dt)
        await self.session.flush()
        record(
            self.session,
            self.actor,
            "data_type.create",
            "data_type",
            dt.uuid,
            f"{app.name} · {code}",
        )
        return dt

    async def _data_type(self, uuid: UUID) -> DataType:
        dt = (
            await self.session.execute(select(DataType).where(DataType.uuid == uuid))
        ).scalar_one_or_none()
        if dt is None:
            raise NotFound("DATA_TYPE_NOT_FOUND", "数据编码不存在")
        return dt

    async def update_data_type(self, uuid: UUID, data: dict[str, Any]) -> DataType:
        dt = await self._data_type(uuid)
        if "name" in data:
            dt.name = require_text(data["name"], "name", "名称", 32)
        if "description" in data:
            dt.description = (data["description"] or "").strip()[:200] or None
        if "admin_operation_code" in data:
            dt.admin_operation_code = await self._check_admin_op(
                dt.app_uuid, data["admin_operation_code"]
            )
        await self.session.flush()
        return dt

    async def delete_data_type(self, uuid: UUID) -> None:
        dt = await self._data_type(uuid)
        n = await self.data_type_count(dt.uuid)
        if n:
            raise Conflict(
                "DATA_TYPE_IN_USE",
                f"还有 {n} 条数据使用该编码，应用删除数据并调用清理接口后才能删除",
                count=n,
            )
        await self.session.delete(dt)
        record(self.session, self.actor, "data_type.delete", "data_type", dt.uuid, dt.code)
        await self.session.flush()
