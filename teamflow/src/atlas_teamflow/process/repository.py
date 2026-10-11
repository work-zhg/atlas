"""产物仓库：建仓库、按团队成员身份同步仓库协作者（产物仓库与 Agent 提交 · §5 §9）。

协作者权限：团队成员（读写）→ pull；团队管理员（Owner）→ pull，
项目开启「允许管理员直接提交」时为 push。
只同步已绑定 Gitee 账号的成员；只管理 TeamFlow 自己设置过的账号（tf_repo_collaborator），
不碰命名空间所有者、机器人账号等其他成员。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import GitAccount, Project, RepoCollaborator, Team
from ..db.types import utcnow
from ..errors import Conflict, TFError
from .repo_host import RepoHost, RepoInfo

__all__ = ["ensure_project_repo", "sync_project_collaborators", "sync_team_collaborators"]

log = logging.getLogger("atlas_teamflow.repo")


async def ensure_project_repo(session: AsyncSession, host: RepoHost, pr: Project) -> RepoInfo:
    """产物仓库就绪（没有就建）。失败时记在项目上并抛 REPO_NOT_READY。"""
    name = pr.repo_name or host.repo_name(pr.uuid)
    if pr.repo_status == "ready" and pr.repo_name:
        return host.info(name)
    team = await session.scalar(select(Team.name).where(Team.uuid == pr.team_uuid))
    try:
        info = await host.ensure_repo(name, f"AI TeamFlow 产物仓库 · {team} / {pr.name}")
    except TFError as exc:
        pr.repo_status, pr.repo_error = "error", exc.message[:300]
        await session.flush()
        raise Conflict("REPO_NOT_READY", f"产物仓库不可用：{exc.message}") from exc
    pr.repo_name, pr.repo_status, pr.repo_error = name, "ready", None
    await session.flush()
    return info


async def _desired(session: AsyncSession, uc: Any, pr: Project) -> dict[str, tuple[UUID, str]]:
    """login → (成员, 权限)。"""
    holders = await uc.team_holders(pr.team_uuid, "WRITE")
    ids = [UUID(h["user"]["id"]) for h in holders]
    accounts = (
        {
            a.user_uuid: a.login
            for a in await session.scalars(select(GitAccount).where(GitAccount.user_uuid.in_(ids)))
        }
        if ids
        else {}
    )
    out: dict[str, tuple[UUID, str]] = {}
    for h in holders:
        uid = UUID(h["user"]["id"])
        login = accounts.get(uid)
        if not login:
            continue
        push = h["permission"] == "OWNER" and pr.allow_admin_commit
        out[login] = (uid, "push" if push else "pull")
    return out


async def sync_project_collaborators(
    session: AsyncSession, uc: Any, host: RepoHost, pr: Project
) -> dict[str, int]:
    """以团队成员身份为准修正仓库协作者（增、改、删）。单个账号失败只记在该行上，不影响其他人。"""
    if pr.repo_status != "ready" or not pr.repo_name or pr.status != "active":
        return {"set": 0, "removed": 0, "failed": 0}
    desired = await _desired(session, uc, pr)
    actual = await host.collaborators(pr.repo_name)
    rows = {
        r.login: r
        for r in await session.scalars(
            select(RepoCollaborator).where(RepoCollaborator.project_uuid == pr.uuid)
        )
    }
    stats = {"set": 0, "removed": 0, "failed": 0}
    for login, (uid, perm) in desired.items():
        if actual.get(login) == "admin":  # 仓库管理员（命名空间所有者等）：不归 TeamFlow 管
            continue
        row = rows.get(login)
        if row is None:
            row = RepoCollaborator(
                project_uuid=pr.uuid, login=login, user_uuid=uid, permission=perm
            )
            session.add(row)
        row.permission, row.user_uuid = perm, uid
        if actual.get(login) == perm and row.synced_at and not row.error:
            continue
        try:
            await host.set_collaborator(pr.repo_name, login, perm)
            row.synced_at, row.error = utcnow(), None
            stats["set"] += 1
        except TFError as exc:
            row.error = exc.message[:300]
            stats["failed"] += 1
    for login, row in rows.items():
        if login in desired:
            continue
        try:
            if login in actual:
                await host.remove_collaborator(pr.repo_name, login)
            await session.execute(delete(RepoCollaborator).where(RepoCollaborator.id == row.id))
            stats["removed"] += 1
        except TFError as exc:
            row.error = exc.message[:300]
            stats["failed"] += 1
    await session.flush()
    return stats


async def sync_team_collaborators(
    session: AsyncSession, uc: Any, host: RepoHost, team_uuid: UUID
) -> None:
    for pr in await session.scalars(
        select(Project).where(Project.team_uuid == team_uuid, Project.status == "active")
    ):
        try:
            await sync_project_collaborators(session, uc, host, pr)
        except TFError as exc:  # 托管平台暂不可用：不挡住团队成员的变更，对账会补齐
            log.warning("同步项目 %s 的仓库协作者失败：%s", pr.uuid, exc.message)
