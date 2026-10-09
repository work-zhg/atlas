"""首次部署：建根部门与首个超级管理员（总体设计 §16）。

★ 只在「还没有任何用户」时允许执行，防止被误用来给自己加超级管理员。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import Actor, record
from .builtin import SUPER_ROLE_CODE, sync_builtin
from .db.models import Grant, Role, User
from .errors import Conflict
from .org.service import OrgService
from .settings import get_settings
from .user.service import UserService

__all__ = ["bootstrap"]


async def bootstrap(
    session: AsyncSession, *, company: str, account: str, name: str, email: str
) -> tuple[str, str]:
    if await session.scalar(select(func.count()).select_from(User)):
        raise Conflict("ALREADY_BOOTSTRAPPED", "已经有用户了，bootstrap 只能在首次部署时执行")
    await sync_builtin(session)
    actor = Actor.system()
    org = OrgService(session, actor)
    root = await org.root() or await org.create_root(company)
    user, temp = await UserService(session, actor, get_settings()).create(
        {"name": name, "account": account, "email": email, "dept_uuid": root.uuid}
    )
    role_uuid = (
        await session.execute(select(Role.uuid).where(Role.code == SUPER_ROLE_CODE))
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
