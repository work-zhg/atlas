"""租户（租户设计 §07 · §11，租户隔离设计 §3、§10）：租户的查询与运营中心的操作。

· 所有租户一律平等，没有平台租户；租户的开通、停用、删除只有运营中心能做（经 api/ops.py）
· 建租户 = 同一事务内建租户、根部门、首个租户超级管理员（临时密码只返回这一次）；
  按运营中心先生成的租户 uuid 幂等：重做时首个管理员还没登录过，就重发临时密码（旧的作废）
· 运营中心只碰租户本身（名字、状态、名额、到期、开通的应用），看不到租户里的人员与授权；
  进租户只为建根部门和首个管理员、删除时清掉全部数据
"""

from __future__ import annotations

import re
from datetime import datetime
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
    AuditEvent,
    Credential,
    DataAcl,
    DataObject,
    DataTypeAcl,
    Dept,
    Grant,
    LoginLog,
    Position,
    PositionRole,
    Role,
    Tenant,
    TenantApp,
    User,
)
from ..db.tenancy import platform_scope, tenant_scope
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound

__all__ = [
    "TENANT_CODE_RE",
    "TenantService",
    "find_by_code",
    "get_tenant",
    "sole_tenant",
    "tenant_usable",
]

TENANT_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def tenant_usable(t: Tenant) -> bool:
    """可用：active 且未到期（到期视同 suspended）。"""
    return t.status == "active" and (t.expires_at is None or t.expires_at > utcnow())


async def get_tenant(session: AsyncSession, uuid: UUID) -> Tenant:
    t = (await session.execute(select(Tenant).where(Tenant.uuid == uuid))).scalar_one_or_none()
    if t is None:
        raise NotFound("TENANT_NOT_FOUND", "租户不存在")
    return t


async def find_by_code(session: AsyncSession, code: str | None) -> Tenant | None:
    code = (code or "").strip().lower()
    if not code:
        return None
    return (await session.execute(select(Tenant).where(Tenant.code == code))).scalar_one_or_none()


async def sole_tenant(session: AsyncSession) -> Tenant | None:
    """只有一个租户时返回它 —— 单租户部署登录不用填企业代码。"""
    rows = list(await session.scalars(select(Tenant).limit(2)))
    return rows[0] if len(rows) == 1 else None


#: 删除租户时按这个顺序删（子表在前）；用户和部门互相引用，单独处理（见 TenantService.delete）
_PURGE_BEFORE_USERS: list[Any] = [
    DataAcl,
    DataTypeAcl,
    DataObject,
    Grant,
    PositionRole,
    Position,
    AppScopeDept,
    TenantApp,
    AppToken,
    Role,  # 本租户自定义的；角色上的操作、菜单随外键级联
]


class TenantService:
    """运营中心经运营接口调用（api/ops.py）。都在跨租户范围里执行，不进入任何租户的上下文，
    除了建首个管理员、补发他的临时密码、删除时清数据。"""

    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    async def get(self, uuid: UUID) -> Tenant:
        with platform_scope(self.session):
            return await get_tenant(self.session, uuid)

    async def users(self, uuid: UUID | None = None) -> dict[UUID, int]:
        """→ {租户: 未停用的人数}（不含数字员工）。只有数字，不含任何人。"""
        stmt = (
            select(User.tenant_uuid, func.count())
            .where(User.status == "active", User.user_type == "human")
            .group_by(User.tenant_uuid)
        )
        if uuid is not None:
            stmt = stmt.where(User.tenant_uuid == uuid)
        with platform_scope(self.session):
            return {t: int(n) for t, n in (await self.session.execute(stmt)).all()}

    async def list(self) -> list[tuple[Tenant, int]]:
        """→ [(租户, 未停用人数)]。"""
        counts = await self.users()
        with platform_scope(self.session):
            tenants = list(await self.session.scalars(select(Tenant).order_by(Tenant.id)))
        return [(t, counts.get(t.uuid, 0)) for t in tenants]

    async def expired(self) -> list[Tenant]:
        """已过到期时间、但还没停用的租户：运营中心据此发起停用（到期走停用流程）。"""
        with platform_scope(self.session):
            return list(
                await self.session.scalars(
                    select(Tenant)
                    .where(Tenant.status == "active", Tenant.expires_at <= utcnow())
                    .order_by(Tenant.expires_at)
                )
            )

    async def create(
        self,
        *,
        uuid: UUID,
        code: str,
        name: str,
        admin: dict[str, Any],
        max_users: int | None = None,
        expires_at: datetime | None = None,
        apps: list[str] | None = None,
        settings: Any,
    ) -> tuple[Tenant, User, str | None, bool]:
        """建租户 + 根部门 + 首个租户超级管理员，开通应用。→ (租户, 管理员, 临时密码, 是否新建)。

        ★ 按 uuid 幂等（运营中心超时后会用同一个 uuid 重做）：已经建过就只补开通应用；首个管理员
          还没登录过就重发临时密码（上一次的可能没送到运营人员手里），登录过就不再给。
        """
        code = (code or "").strip().lower()
        with platform_scope(self.session):
            existing = (
                await self.session.execute(select(Tenant).where(Tenant.uuid == uuid))
            ).scalar_one_or_none()
        if existing is not None:
            if existing.code != code:
                raise Conflict("TENANT_UUID_CONFLICT", "这个租户 uuid 已被别的企业代码使用")
            await self._enable_apps(existing, apps or [])
            admin_user, temp = await self._reissue(existing, settings, strict=False)
            return existing, admin_user, temp, False

        from ..builtin import SUPER_ROLE_CODE
        from ..org.service import OrgService
        from ..user.service import UserService

        if not TENANT_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED",
                "企业代码：小写字母开头，小写字母、数字与 -，2–32 位",
                field="code",
            )
        with platform_scope(self.session):
            if await find_by_code(self.session, code):
                raise Conflict("TENANT_CODE_EXISTS", "企业代码已存在")
        tenant = Tenant(
            uuid=uuid,
            code=code,
            name=require_text(name, "name", "租户名称", 64),
            status="active",
            max_users=_quota(max_users),
            expires_at=expires_at,
        )
        self.session.add(tenant)
        await self.session.flush()
        record(
            self.session, self.actor, "tenant.create", "tenant", tenant.uuid, tenant.name, code=code
        )

        # 在新租户里建根部门与首个超级管理员
        with tenant_scope(self.session, tenant.uuid):
            org = OrgService(self.session, self.actor)
            root = await org.create_root(tenant.name)
            user, temp = await UserService(self.session, self.actor, settings).create(
                {
                    "name": admin.get("name"),
                    "account": admin.get("account"),
                    "email": admin.get("email"),
                    "dept_uuid": root.uuid,
                }
            )
            role_uuid = (
                await self.session.execute(
                    select(Role.uuid).where(
                        Role.code == SUPER_ROLE_CODE, Role.tenant_uuid.is_(None)
                    )
                )
            ).scalar_one()
            self.session.add(
                Grant(kind="role", role_uuid=role_uuid, subject_type="user", user_uuid=user.uuid)
            )
            root.leader_uuid = user.uuid
            record(
                self.session,
                self.actor,
                "grant.add_role",
                "role",
                role_uuid,
                "超级管理员",
                subjects=[user.name],
                tenant_created=True,
            )
            await self.session.flush()
        tenant.admin_uuid = user.uuid
        await self._enable_apps(tenant, apps or [])
        await self.session.flush()
        return tenant, user, temp, True

    async def update(self, uuid: UUID, data: dict[str, Any]) -> Tenant:
        """改名字、人员名额、到期时间。状态不在这里改：停用 / 恢复见 set_blocked。"""
        t = await self.get(uuid)
        changes: dict[str, Any] = {}
        if "name" in data:
            name = require_text(data["name"], "name", "租户名称", 64)
            if name != t.name:
                changes["name"] = [t.name, name]
                t.name = name
        if "max_users" in data:
            t.max_users = _quota(data["max_users"])
            changes["max_users"] = t.max_users
        if "expires_at" in data:
            if data["expires_at"] is not None:
                _guard_system(t, "设到期时间")
            t.expires_at = data["expires_at"]
            changes["expires_at"] = t.expires_at.isoformat() if t.expires_at else None
        if changes:
            t.version += 1
            record(
                self.session, self.actor, "tenant.update", "tenant", t.uuid, t.name, changes=changes
            )
        await self.session.flush()
        return t

    async def set_blocked(self, uuid: UUID, blocked: bool) -> Tenant:
        """挡住 / 放开这个租户的入口：登录、网关校验、各系统调开放接口都看它。幂等。

        ★ 只挡新请求。在跑的后台工作由运营中心随后通知各系统挂起（租户隔离设计 §10）。
        """
        t = await self.get(uuid)
        if blocked:
            _guard_system(t, "停用")
        status = "suspended" if blocked else "active"
        if t.status != status:
            record(
                self.session,
                self.actor,
                "tenant.block" if blocked else "tenant.unblock",
                "tenant",
                t.uuid,
                t.name,
                changes={"status": [t.status, status]},
            )
            t.status = status
            t.version += 1
        if blocked:
            await self._revoke_tokens(t.uuid)
        await self.session.flush()
        return t

    async def reissue_admin_password(self, uuid: UUID, settings: Any) -> tuple[User, str]:
        """补发首个管理员的临时密码（旧的作废）。他登录过之后拒绝：那时运营中心再也拿不到租户账号。"""
        t = await self.get(uuid)
        user, temp = await self._reissue(t, settings, strict=True)
        assert temp is not None
        return user, temp

    async def _reissue(self, t: Tenant, settings: Any, *, strict: bool) -> tuple[User, str | None]:
        from ..user.service import UserService

        if t.admin_uuid is None:
            raise Conflict("ADMIN_UNKNOWN", "这个租户不是运营中心建的，没有记首个管理员")
        with tenant_scope(self.session, t.uuid):
            svc = UserService(self.session, self.actor, settings)
            user = await svc.get(t.admin_uuid)
            if user.last_login_at is not None:
                if strict:
                    raise Conflict("ADMIN_ACTIVATED", "首个管理员已经登录过，不能再补发临时密码")
                return user, None
            _user, temp, _expires = await svc.reset_password(user.uuid)
        return user, temp

    async def delete(self, uuid: UUID) -> dict[str, int]:
        """删除租户：人、部门、岗位、角色、授权、数据权限、数字员工、日志，最后是租户本身。

        ★ 只删已停用的：先挡住入口，保证删的时候没有人还在写。
        ★ 幂等：已经删了返回空。数据量是一个组织的人和授权，一个事务删完。
        ★ 在跨租户范围里用 Core 语句按 tenant_uuid 删（用户、部门互相引用，先断开部门负责人）。
        """
        with platform_scope(self.session):
            t = (
                await self.session.execute(select(Tenant).where(Tenant.uuid == uuid))
            ).scalar_one_or_none()
            if t is None:
                return {}
            _guard_system(t, "删除")
            if t.status == "active":
                raise Conflict("TENANT_ACTIVE", "只能删除已停用的租户：先停用")
            deleted: dict[str, int] = {}

            async def run(name: str, stmt: Any) -> None:
                result = await self.session.execute(stmt)
                n = int(getattr(result, "rowcount", 0) or 0)
                if n:
                    deleted[name] = deleted.get(name, 0) + n

            for model in _PURGE_BEFORE_USERS:
                table = model.__table__
                await run(table.name, table.delete().where(table.c.tenant_uuid == t.uuid))
            users = select(User.__table__.c.uuid).where(User.__table__.c.tenant_uuid == t.uuid)
            await run(
                Credential.__tablename__,
                Credential.__table__.delete().where(Credential.__table__.c.user_uuid.in_(users)),
            )
            dept = Dept.__table__
            await self.session.execute(
                dept.update().where(dept.c.tenant_uuid == t.uuid).values(leader_uuid=None)
            )
            # 登录会话随用户级联删除；用户之间（数字员工 → 负责人）、部门之间（上下级）的引用
            # 在同一条语句里删完，外键在语句结束时才检查
            for model in (User, Dept, LoginLog, AuditEvent):
                table = model.__table__
                await run(table.name, table.delete().where(table.c.tenant_uuid == t.uuid))
            await self.session.execute(
                Tenant.__table__.delete().where(Tenant.__table__.c.uuid == t.uuid)
            )
            record(
                self.session,
                self.actor,
                "tenant.delete",
                "tenant",
                t.uuid,
                t.name,
                code=t.code,
                deleted=deleted,
            )
            await self.session.flush()
        return deleted

    async def apps(self, uuid: UUID) -> list[tuple[App, TenantApp | None]]:
        """全部非内置应用，以及该租户的开通情况。"""
        await self.get(uuid)
        apps = list(
            await self.session.scalars(
                select(App).where(App.is_builtin.is_(False)).order_by(App.id)
            )
        )
        with tenant_scope(self.session, uuid):
            opened = {ta.app_uuid: ta for ta in await self.session.scalars(select(TenantApp))}
        return [(a, opened.get(a.uuid)) for a in apps]

    async def set_app(
        self, uuid: UUID, app_uuid: UUID, enabled: bool, *, scope_all: bool | None = None
    ) -> None:
        """给租户开通 / 关闭应用。开通后范围默认无人（scope_all=False），由租户自己配；
        scope_all=True 开成全员可访问（运营中心给系统租户开通自己时用）。"""
        t = await self.get(uuid)
        app = (
            await self.session.execute(select(App).where(App.uuid == app_uuid))
        ).scalar_one_or_none()
        if app is None or app.is_builtin:
            raise NotFound("APP_NOT_FOUND", "应用不存在")
        await self._switch_app(t, app, enabled, scope_all=scope_all)

    async def _enable_apps(self, t: Tenant, codes: list[str]) -> None:
        for code in dict.fromkeys(c.strip() for c in codes if c and c.strip()):
            app = (
                await self.session.execute(
                    select(App).where(App.code == code, App.is_builtin.is_(False))
                )
            ).scalar_one_or_none()
            if app is None:
                raise Invalid("APP_NOT_FOUND", f"应用 {code} 不存在", field="apps")
            await self._switch_app(t, app, True)

    async def _switch_app(
        self, t: Tenant, app: App, enabled: bool, *, scope_all: bool | None = None
    ) -> None:
        if enabled and app.system_only and not t.is_system:
            raise Conflict("SYSTEM_ONLY_APP", f"{app.name} 只能开给系统租户")
        with tenant_scope(self.session, t.uuid):
            row = (
                await self.session.execute(select(TenantApp).where(TenantApp.app_uuid == app.uuid))
            ).scalar_one_or_none()
            if enabled and row is None:
                row = TenantApp(app_uuid=app.uuid, enabled_at=utcnow())
                self.session.add(row)
            elif row is not None:
                row.status = "active" if enabled else "disabled"
            if enabled and scope_all is not None and row is not None:
                row.scope_all = scope_all
            await self.session.flush()
        if not enabled:
            await self._revoke_tokens(t.uuid, app.uuid)
        record(
            self.session,
            self.actor,
            "tenant.app_enable" if enabled else "tenant.app_disable",
            "tenant",
            t.uuid,
            f"{t.name} · {app.name}",
        )
        await self.session.flush()

    async def _revoke_tokens(self, tenant_uuid: UUID, app_uuid: UUID | None = None) -> None:
        stmt = AppToken.__table__.delete().where(AppToken.tenant_uuid == tenant_uuid)
        if app_uuid is not None:
            stmt = stmt.where(AppToken.app_uuid == app_uuid)
        await self.session.execute(stmt)


def _guard_system(t: Tenant, what: str) -> None:
    """系统租户不能停用、删除、设到期：运营人员都靠它登录运营中心，停了就把自己锁在门外。"""
    if t.is_system:
        raise Conflict("SYSTEM_TENANT", f"系统租户不能{what}：运营人员靠它登录运营中心")


def _quota(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise Invalid("VALIDATION_FAILED", "人员配额必须是正整数", field="max_users") from exc
    if n <= 0:
        raise Invalid("VALIDATION_FAILED", "人员配额必须是正整数", field="max_users")
    return n
