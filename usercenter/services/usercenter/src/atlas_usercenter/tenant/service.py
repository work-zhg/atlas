"""租户（租户设计 §07 · §11）：租户的查询与平台侧管理。

· 平台管理员（平台租户内、持有 tenant:manage）建租户、设配额与状态、给租户开通应用
· 建租户 = 同一事务内建租户、根部门、首个租户超级管理员（临时密码只返回这一次）
· 平台管理员默认看不到客户租户内的人员与授权 —— 这里只碰租户本身与开通关系
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
from ..db.models import App, Grant, Role, Tenant, TenantApp, User
from ..db.tenancy import current_tenant, platform_scope, tenant_scope
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid, NotFound

__all__ = [
    "TENANT_CODE_RE",
    "TenantService",
    "current_tenant_row",
    "find_by_code",
    "get_tenant",
    "require_platform",
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


async def current_tenant_row(session: AsyncSession) -> Tenant:
    tid = current_tenant(session)
    if tid is None:
        raise RuntimeError("没有租户上下文")
    return await get_tenant(session, tid)


async def find_by_code(session: AsyncSession, code: str | None) -> Tenant | None:
    code = (code or "").strip().lower()
    if not code:
        return None
    return (await session.execute(select(Tenant).where(Tenant.code == code))).scalar_one_or_none()


async def sole_tenant(session: AsyncSession) -> Tenant | None:
    """只有一个租户时返回它 —— 单租户部署登录不用填企业代码。"""
    rows = list(await session.scalars(select(Tenant).limit(2)))
    return rows[0] if len(rows) == 1 else None


class TenantService:
    """平台级：只在平台租户内、持有 tenant:* 操作码的人调用（接口层鉴权）。"""

    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    async def list(self) -> list[tuple[Tenant, int]]:
        """→ [(租户, 未停用人数)]。人数跨租户统计，在平台范围内算。"""
        with platform_scope(self.session):
            counts = dict(
                (
                    await self.session.execute(
                        select(User.tenant_uuid, func.count())
                        .where(User.status == "active")
                        .group_by(User.tenant_uuid)
                    )
                ).all()
            )
        tenants = list(
            await self.session.scalars(select(Tenant).order_by(Tenant.kind.desc(), Tenant.id))
        )
        return [(t, int(counts.get(t.uuid, 0))) for t in tenants]

    async def create(
        self,
        *,
        code: str,
        name: str,
        admin: dict[str, Any],
        max_users: int | None = None,
        expires_at: datetime | None = None,
        settings: Any,
    ) -> tuple[Tenant, User, str]:
        """建租户 + 根部门 + 首个租户超级管理员。→ (租户, 管理员, 临时密码)。"""
        from ..builtin import SUPER_ROLE_CODE
        from ..org.service import OrgService
        from ..user.service import UserService

        code = (code or "").strip().lower()
        if not TENANT_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED",
                "企业代码：小写字母开头，小写字母、数字与 -，2–32 位",
                field="code",
            )
        if await find_by_code(self.session, code):
            raise Conflict("TENANT_CODE_EXISTS", "企业代码已存在")
        tenant = Tenant(
            code=code,
            name=require_text(name, "name", "租户名称", 64),
            kind="customer",
            status="active",
            max_users=_quota(max_users),
            expires_at=expires_at,
            created_by=self.actor.user_uuid,
            updated_by=self.actor.user_uuid,
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
        return tenant, user, temp

    async def update(self, uuid: UUID, data: dict[str, Any]) -> Tenant:
        t = await get_tenant(self.session, uuid)
        changes: dict[str, Any] = {}
        if "name" in data:
            name = require_text(data["name"], "name", "租户名称", 64)
            if name != t.name:
                changes["name"] = [t.name, name]
                t.name = name
        if "status" in data:
            status = str(data["status"])
            if status not in ("active", "suspended", "closed"):
                raise Invalid("VALIDATION_FAILED", "状态只能是 active / suspended / closed")
            if t.kind == "platform" and status != "active":
                raise Conflict("PLATFORM_TENANT", "平台租户不能停用")
            if status != t.status:
                changes["status"] = [t.status, status]
                t.status = status
                if status != "active":
                    await self._revoke_tokens(t.uuid)
        if "max_users" in data:
            t.max_users = _quota(data["max_users"])
            changes["max_users"] = t.max_users
        if "expires_at" in data:
            if t.kind == "platform" and data["expires_at"] is not None:
                raise Conflict("PLATFORM_TENANT", "平台租户不设到期")
            t.expires_at = data["expires_at"]
            changes["expires_at"] = t.expires_at.isoformat() if t.expires_at else None
        if changes:
            t.version += 1
            t.updated_by = self.actor.user_uuid
            record(
                self.session, self.actor, "tenant.update", "tenant", t.uuid, t.name, changes=changes
            )
        await self.session.flush()
        return t

    async def apps(self, uuid: UUID) -> list[tuple[App, TenantApp | None]]:
        """全部非内置应用，以及该租户的开通情况。"""
        await get_tenant(self.session, uuid)
        apps = list(
            await self.session.scalars(
                select(App).where(App.is_builtin.is_(False)).order_by(App.id)
            )
        )
        with tenant_scope(self.session, uuid):
            opened = {ta.app_uuid: ta for ta in await self.session.scalars(select(TenantApp))}
        return [(a, opened.get(a.uuid)) for a in apps]

    async def set_app(self, uuid: UUID, app_uuid: UUID, enabled: bool) -> None:
        """平台给租户开通 / 关闭应用。开通后范围默认无人（scope_all=False），由租户自己配。"""
        t = await get_tenant(self.session, uuid)
        app = (
            await self.session.execute(select(App).where(App.uuid == app_uuid))
        ).scalar_one_or_none()
        if app is None or app.is_builtin:
            raise NotFound("APP_NOT_FOUND", "应用不存在")
        with tenant_scope(self.session, t.uuid):
            row = (
                await self.session.execute(select(TenantApp).where(TenantApp.app_uuid == app.uuid))
            ).scalar_one_or_none()
            if enabled and row is None:
                self.session.add(
                    TenantApp(
                        app_uuid=app.uuid, enabled_by=self.actor.user_uuid, enabled_at=utcnow()
                    )
                )
            elif row is not None:
                row.status = "active" if enabled else "disabled"
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
        from ..db.models import AppToken

        stmt = AppToken.__table__.delete().where(AppToken.tenant_uuid == tenant_uuid)
        if app_uuid is not None:
            stmt = stmt.where(AppToken.app_uuid == app_uuid)
        await self.session.execute(stmt)


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


async def require_platform(session: AsyncSession) -> None:
    """平台级接口的第二道闸：当前租户必须是平台租户（操作码之外再确认一次）。"""
    if (await current_tenant_row(session)).kind != "platform":
        raise Forbidden("PLATFORM_ONLY", "只有平台租户可以执行此操作")
