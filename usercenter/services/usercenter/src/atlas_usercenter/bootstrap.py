"""首次部署：建根部门与首个超级管理员（总体设计 §16）。

★ 只在「还没有任何用户」时允许执行，防止被误用来给自己加超级管理员。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import Actor, record
from .builtin import SUPER_ROLE_CODE, sync_builtin
from .db.models import Grant, Role, Tenant, User
from .db.tenancy import platform_scope, tenant_scope
from .errors import Conflict
from .org.service import OrgService
from .settings import get_settings
from .user.service import UserService

__all__ = ["bootstrap"]


async def _platform_tenant(session: AsyncSession, company: str) -> Tenant:
    """平台租户（全局唯一）。迁移会建它；测试清空全部表后由这里补建。"""
    tenant = (
        await session.execute(select(Tenant).where(Tenant.kind == "platform"))
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(code="default", name=company, kind="platform", status="active")
        session.add(tenant)
        await session.flush()
    return tenant


async def bootstrap(
    session: AsyncSession, *, company: str, account: str, name: str, email: str
) -> tuple[str, str]:
    """在平台租户里建根部门与首个超级管理员（他同时是平台管理员，租户设计 §13）。"""
    with platform_scope(session):
        if await session.scalar(select(func.count()).select_from(User)):
            raise Conflict("ALREADY_BOOTSTRAPPED", "已经有用户了，bootstrap 只能在首次部署时执行")
        await sync_builtin(session)
        tenant = await _platform_tenant(session, company)
    with tenant_scope(session, tenant.uuid):
        return await _bootstrap_in_tenant(session, company, account, name, email)


async def _bootstrap_in_tenant(
    session: AsyncSession, company: str, account: str, name: str, email: str
) -> tuple[str, str]:
    actor = Actor.system()
    org = OrgService(session, actor)
    root = await org.root() or await org.create_root(company)
    user, temp = await UserService(session, actor, get_settings()).create(
        {"name": name, "account": account, "email": email, "dept_uuid": root.uuid}
    )
    role_uuid = (
        await session.execute(
            select(Role.uuid).where(Role.code == SUPER_ROLE_CODE, Role.tenant_uuid.is_(None))
        )
    ).scalar_one()
    session.add(Grant(kind="role", role_uuid=role_uuid, subject_type="user", user_uuid=user.uuid))
    root.leader_uuid = user.uuid
    record(
        session,
        actor,
        "grant.add_role",
        "role",
        role_uuid,
        "超级管理员",
        subjects=[user.name],
        bootstrap=True,
    )
    await session.flush()
    return user.account, temp
