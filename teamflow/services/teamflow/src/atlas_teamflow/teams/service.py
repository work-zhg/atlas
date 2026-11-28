"""TeamService：团队领域的唯一入口（c4-model/teamflow/code-teams.html）。

    团队        建、改、归档；名字在租户内唯一
    成员与级别  存在用户中心（「团队」数据编码），以当前用户为操作人写；写完广播，各副本作废级别缓存
    数字员工    只接本租户里自己是 Owner 的、以数字员工为权限的智能体（记为接入人）；只存引用和快照
    使用权      以接入人的身份给流程里的人授「使用」，已经有的不重复授，不自动收回

★ 权限（谁能建团队、谁能改）由入口问访问控制判过，这里只收调用人的 uid。
★ 团队不读项目：移除成员、数字员工不改项目配置，项目用的时候按当前级别和数字员工过滤。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record
from ..coord import coordinator
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from .agents import AgentCard, AgentDirectory
from .members import TeamMember, TeamMembers
from .models import Team, TeamAgent

__all__ = ["TeamService"]


def _text(value: str | None, label: str, max_len: int, *, required: bool = True) -> str | None:
    v = (value or "").strip()
    if not v:
        if required:
            raise Invalid("VALIDATION_FAILED", f"请填写{label}")
        return None
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符")
    return v


class TeamService:
    def __init__(self, session: AsyncSession, members: TeamMembers, agents: AgentDirectory) -> None:
        self.session = session
        self.members_acl = members
        self.directory = agents

    # ───────────────────────────── 团队

    async def get(self, team_uuid: UUID) -> Team:
        t = await self.session.scalar(select(Team).where(Team.uuid == team_uuid))
        if t is None:
            raise NotFound("TEAM_NOT_FOUND", "团队不存在")
        return t

    async def list(self, only: set[UUID] | None, *, include_archived: bool = False) -> list[Team]:
        """only = None 表示全部（有「全部团队」级别的人）。"""
        stmt = select(Team).order_by(Team.id)
        if not include_archived:
            stmt = stmt.where(Team.status == "active")
        if only is not None:
            if not only:
                return []
            stmt = stmt.where(Team.uuid.in_(only))
        return list(await self.session.scalars(stmt))

    async def _check_name(self, name: str, exclude: UUID | None = None) -> None:
        stmt = select(Team.uuid).where(Team.name == name)
        if exclude:
            stmt = stmt.where(Team.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "团队名称已存在")

    async def create(
        self, name: str, description: str | None, members: list[dict[str, Any]], *, actor: UUID
    ) -> Team:
        """先建团队行（未提交），再一次写入用户中心：建的人 = Owner、初始成员按给的级别
        （默认读写）。
        用户中心写失败，团队行随请求回滚。"""
        clean = _text(name, "团队名称", 64)
        assert clean is not None
        await self._check_name(clean)
        t = Team(
            name=clean,
            description=_text(description, "说明", 500, required=False),
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(t)
        await self.session.flush()
        await self.members_acl.first_grant(t.uuid, t.name, actor, members)
        record(self.session, actor, "team.create", "team", t.uuid, t.name, members=len(members))
        return t

    async def update(
        self,
        team_uuid: UUID,
        *,
        name: str | None,
        description: str | None,
        version: int | None,
        actor: UUID,
    ) -> Team:
        t = await self.get(team_uuid)
        if version is not None and version != t.version:
            raise Conflict("STALE_ROW_VERSION", "团队已被他人修改，请刷新后重试")
        if name is not None:
            clean = _text(name, "团队名称", 64)
            assert clean is not None
            if clean != t.name:
                await self._check_name(clean, exclude=t.uuid)
                t.name = clean
                await self.members_acl.rename(t.uuid, clean)
        if description is not None:
            t.description = _text(description, "说明", 500, required=False)
        t.version += 1
        t.updated_by = actor
        record(self.session, actor, "team.update", "team", t.uuid, t.name)
        await self.session.flush()
        return t

    async def archive(self, team_uuid: UUID, archived: bool, *, actor: UUID) -> Team:
        t = await self.get(team_uuid)
        status = "archived" if archived else "active"
        if t.status != status:
            t.status = status
            t.version += 1
            t.updated_by = actor
            record(
                self.session,
                actor,
                f"team.{'archive' if archived else 'unarchive'}",
                "team",
                t.uuid,
                t.name,
            )
            await self.session.flush()
        return t

    async def accepting(self, team_uuid: UUID) -> bool:
        """还能不能用这个团队发起流程。"""
        return (await self.get(team_uuid)).status == "active"

    # ───────────────────────────── 成员与级别

    async def members(self, team_uuid: UUID, min_level: str = "READ") -> list[TeamMember]:
        await self.get(team_uuid)
        return await self.members_acl.holders(team_uuid, min_level)

    async def grants(self, team_uuid: UUID) -> list[dict[str, Any]]:
        await self.get(team_uuid)
        return await self.members_acl.grants(team_uuid)

    async def add_members(
        self, team_uuid: UUID, subjects: list[dict[str, Any]], level: str, *, actor: UUID
    ) -> None:
        t = await self.get(team_uuid)
        if not subjects:
            raise Invalid("VALIDATION_FAILED", "请选择成员")
        await self.members_acl.grant(t.uuid, t.name, subjects, level, operator=actor)
        coordinator().team_changed(t.uuid)
        record(
            self.session,
            actor,
            "team.member_add",
            "team",
            t.uuid,
            t.name,
            count=len(subjects),
            level=level,
        )

    async def set_member(self, team_uuid: UUID, acl_id: str, level: str, *, actor: UUID) -> None:
        t = await self.get(team_uuid)
        await self.members_acl.set_level(t.uuid, acl_id, level, operator=actor)
        coordinator().team_changed(t.uuid)
        record(self.session, actor, "team.member_level", "team", t.uuid, t.name, level=level)

    async def remove_member(self, team_uuid: UUID, acl_id: str, *, actor: UUID) -> None:
        t = await self.get(team_uuid)
        row = await self.members_acl.revoke(t.uuid, acl_id, operator=actor)
        coordinator().team_changed(t.uuid)
        record(
            self.session,
            actor,
            "team.member_remove",
            "team",
            t.uuid,
            t.name,
            subject=(row.get("subject") or {}).get("name"),
        )

    # ───────────────────────────── 数字员工

    async def agents(self, team_uuid: UUID) -> list[TeamAgent]:
        await self.get(team_uuid)
        return list(
            await self.session.scalars(
                select(TeamAgent).where(TeamAgent.team_uuid == team_uuid).order_by(TeamAgent.id)
            )
        )

    async def agent(self, team_uuid: UUID, agent_uuid: UUID) -> TeamAgent:
        ta = await self.session.scalar(
            select(TeamAgent).where(TeamAgent.uuid == agent_uuid, TeamAgent.team_uuid == team_uuid)
        )
        if ta is None:
            raise NotFound("AGENT_NOT_FOUND", "团队里没有这个数字员工")
        return ta

    async def candidates(self, team_uuid: UUID, q: str | None) -> list[tuple[AgentCard, bool]]:
        """能接入的数字员工（当前用户看得到的、以数字员工为权限的），和是否已经接入。"""
        await self.get(team_uuid)
        added = set(
            await self.session.scalars(
                select(TeamAgent.atlas_agent_id).where(TeamAgent.team_uuid == team_uuid)
            )
        )
        return [(c, c.atlas_agent_id in added) for c in await self.directory.list(q)]

    async def add_agent(self, team_uuid: UUID, atlas_agent_id: str, *, actor: UUID) -> TeamAgent:
        t = await self.get(team_uuid)
        card = await self.directory.detail(atlas_agent_id)
        if card is None or card.status != "enabled":
            raise Invalid("AGENT_UNAVAILABLE", "智能体不存在或没有启用")
        if not card.is_digital_employee:
            raise Invalid("NOT_DIGITAL_EMPLOYEE", "只能接入以数字员工为权限的智能体")
        if await self.directory.my_level(atlas_agent_id) != "OWNER":
            raise Invalid(
                "AGENT_OWNER_REQUIRED",
                "只有数字员工的 Owner 能把它接入团队（之后要以你的身份给流程里的人授使用权）",
            )
        if await self.session.scalar(
            select(TeamAgent.uuid).where(
                TeamAgent.team_uuid == t.uuid, TeamAgent.atlas_agent_id == card.atlas_agent_id
            )
        ):
            raise Conflict("AGENT_EXISTS", "这个数字员工已经在团队里")
        ta = TeamAgent(
            team_uuid=t.uuid,
            atlas_agent_id=card.atlas_agent_id,
            name=card.name[:100],
            kind=card.kind,
            model=card.model,
            description=card.description,
            added_by=actor,
        )
        self.session.add(ta)
        record(self.session, actor, "team.agent_add", "team", t.uuid, t.name, agent=ta.name)
        await self.session.flush()
        return ta

    async def remove_agent(self, team_uuid: UUID, agent_uuid: UUID, *, actor: UUID) -> None:
        """移除不改项目配置：项目用的时候按团队当前的数字员工过滤，被移除的就算「还差」。"""
        t = await self.get(team_uuid)
        ta = await self.agent(t.uuid, agent_uuid)
        await self.session.delete(ta)
        record(self.session, actor, "team.agent_remove", "team", t.uuid, t.name, agent=ta.name)
        await self.session.flush()

    async def agent_card(
        self, team_uuid: UUID, agent_uuid: UUID
    ) -> tuple[TeamAgent, AgentCard | None]:
        ta = await self.agent(team_uuid, agent_uuid)
        return ta, await self.directory.detail(ta.atlas_agent_id)

    async def sync_agents(self, team_uuid: UUID) -> list[TeamAgent]:
        """按 Agent 平台刷新快照和能不能用（删了、停用了、改成以人为权限的 → 不能用）。"""
        rows = await self.agents(team_uuid)
        for ta in rows:
            card = await self.directory.detail(ta.atlas_agent_id)
            ta.available = bool(card and card.available)
            if card:
                ta.name, ta.kind, ta.model = card.name[:100], card.kind, card.model
                ta.description = card.description
            ta.synced_at = utcnow()
        await self.session.flush()
        return rows

    async def grant_use(self, team_uuid: UUID, agent_uuids: list[UUID], uids: list[UUID]) -> None:
        """以各数字员工的接入人的身份，给这些人授「使用」。接入人已经不是 Owner 时 Agent 平台拒绝，
        错误原样抛出（发起流程失败，提示团队管理员重新接入）。"""
        if not agent_uuids or not uids:
            return
        rows = list(
            await self.session.scalars(
                select(TeamAgent).where(
                    TeamAgent.team_uuid == team_uuid, TeamAgent.uuid.in_(agent_uuids)
                )
            )
        )
        for ta in rows:
            await self.directory.share_use(ta.atlas_agent_id, uids, operator=ta.added_by)
