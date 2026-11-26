"""首次部署：建第一个租户（企业代码 default）、根部门与首个超级管理员（总体设计 §16）。

★ 只在「还没有任何用户」时允许执行，防止被误用来给自己加超级管理员。
★ 它是普通租户（租户隔离设计 §0：没有平台租户）。再建别的租户、给租户开通应用、维护应用目录
  都在运营中心做（运营接口 api/ops.py）；单租户部署只用它就够了。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import Actor, record
from .builtin import SUPER_ROLE_CODE, sync_builtin
from .db.models import Grant, Role, Tenant, User
from .db.tenancy import platform_scope, tenant_scope
from .errors import Conflict, Invalid
from .org.service import OrgService
from .settings import get_settings
from .tenant.service import TENANT_CODE_RE
from .user.service import UserService

__all__ = ["bootstrap", "bootstrap_system"]


#: 第一个租户的企业代码（u0004 迁移给存量数据建的也是它）
FIRST_TENANT_CODE = "default"


async def _first_tenant(session: AsyncSession, company: str) -> Tenant:
    """第一个租户。升级上来的库迁移时已经有了；新库、测试清空全部表后由这里建。"""
    tenant = (
        await session.execute(select(Tenant).where(Tenant.code == FIRST_TENANT_CODE))
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(code=FIRST_TENANT_CODE, name=company, status="active")
        session.add(tenant)
        await session.flush()
    return tenant


async def bootstrap(
    session: AsyncSession, *, company: str, account: str, name: str, email: str
) -> tuple[str, str]:
    """在第一个租户里建根部门与首个超级管理员（他只管这个租户）。"""
    with platform_scope(session):
        if await session.scalar(select(func.count()).select_from(User)):
            raise Conflict("ALREADY_BOOTSTRAPPED", "已经有用户了，bootstrap 只能在首次部署时执行")
        await sync_builtin(session)
        tenant = await _first_tenant(session, company)
    with tenant_scope(session, tenant.uuid):
        return await _bootstrap_in_tenant(session, tenant, company, account, name, email)


async def bootstrap_system(
    session: AsyncSession, *, code: str, company: str, account: str, name: str, email: str
) -> tuple[str, str]:
    """建系统租户（运营团队所在的租户，c4-model/opscenter/design-opscenter.html）：根部门与首个
    超级管理员。他在用户中心里给运营人员开账号、授运营模块。

    ★ 只能有一个系统租户、只能用这个部署命令建：运营接口改不了 is_system（否则运营中心能给任何
      租户开运营权限）。不要拿原来的平台租户改：它有业务数据，而系统租户永远删不掉。
    """
    with platform_scope(session):
        await sync_builtin(session)
        if await session.scalar(select(Tenant.uuid).where(Tenant.is_system.is_(True))):
            raise Conflict("SYSTEM_TENANT_EXISTS", "已经有系统租户了")
        if not TENANT_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED", "企业代码：小写字母开头，小写字母、数字与 -，2–32 位"
            )
        if await session.scalar(select(Tenant.uuid).where(Tenant.code == code)):
            raise Conflict("TENANT_CODE_EXISTS", f"企业代码 {code} 已被占用")
        tenant = Tenant(code=code, name=company, status="active", is_system=True)
        session.add(tenant)
        await session.flush()
    with tenant_scope(session, tenant.uuid):
        return await _bootstrap_in_tenant(session, tenant, company, account, name, email)


async def _bootstrap_in_tenant(
    session: AsyncSession, tenant: Tenant, company: str, account: str, name: str, email: str
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
    tenant.admin_uuid = user.uuid
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
