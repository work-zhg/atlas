"""ProjectService：项目的唯一入口（c4-model/teamflow/code-projects.html）。

    项目        建、改、归档；名字在团队内唯一
    绑定模板    可以绑多个；跟随模板的最新发布版
    分配角色    按模板给模板角色配成员（可以多个）和一个数字员工；只读成员不能担任
    配齐        现算：按最新发布版和团队当前的成员、数字员工；执行角色要有人和数字员工，
                只评审的要有人
    解析角色    给流程运行「现在谁是这个角色」（成员按团队当前级别滤掉只读和已移除的）
    使用权      这个模板各角色当前的成员 × 各执行角色配的数字员工，经团队以接入人的身份授「使用」

★ 模板角色是模板里的概念，和用户中心的系统角色无关；分配是项目里的数据，不写进用户中心。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import structure
from ..audit import record
from ..errors import Conflict, Invalid, NotFound, TFError
from ..teams.service import TeamService
from ..templates.service import TemplateService
from .models import Project, ProjectRole, ProjectTemplate

__all__ = ["ProjectService", "Readiness", "RoleTable"]

log = logging.getLogger("atlas_teamflow.projects")


@dataclass(frozen=True)
class Readiness:
    template_uuid: UUID
    version_no: int
    #: [(角色键, 缺什么：no_member / no_agent)]
    missing: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.version_no > 0 and not self.missing


@dataclass(frozen=True)
class RoleTable:
    """现在谁是这个角色：成员（已按团队当前级别过滤）、数字员工（团队数字员工的 uuid 和
    Atlas 编号）。"""

    version_no: int
    members: dict[str, list[UUID]]
    agents: dict[str, tuple[UUID, str] | None]

    def people(self) -> list[UUID]:
        out: list[UUID] = []
        for uids in self.members.values():
            out.extend(u for u in uids if u not in out)
        return out


class ProjectService:
    def __init__(
        self, session: AsyncSession, teams: TeamService, templates: TemplateService
    ) -> None:
        self.session = session
        self.teams = teams
        self.templates = templates

    # ───────────────────────────── 项目

    async def get(self, project_uuid: UUID) -> Project:
        p = await self.session.scalar(select(Project).where(Project.uuid == project_uuid))
        if p is None:
            raise NotFound("PROJECT_NOT_FOUND", "项目不存在")
        return p

    async def of_team(self, team_uuid: UUID, *, include_archived: bool = False) -> list[Project]:
        stmt = select(Project).where(Project.team_uuid == team_uuid).order_by(Project.id)
        if not include_archived:
            stmt = stmt.where(Project.status == "active")
        return list(await self.session.scalars(stmt))

    async def _check_name(self, team_uuid: UUID, name: str, exclude: UUID | None = None) -> str:
        name = (name or "").strip()
        if not name or len(name) > 64:
            raise Invalid("VALIDATION_FAILED", "项目名称必填，最多 64 个字")
        stmt = select(Project.uuid).where(Project.team_uuid == team_uuid, Project.name == name)
        if exclude:
            stmt = stmt.where(Project.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "团队里已经有同名的项目")
        return name

    async def create(
        self, team_uuid: UUID, name: str, description: str | None, *, actor: UUID
    ) -> Project:
        await self.teams.get(team_uuid)
        p = Project(
            team_uuid=team_uuid,
            name=await self._check_name(team_uuid, name),
            description=(description or "").strip()[:500] or None,
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(p)
        record(self.session, actor, "project.create", "project", p.uuid, p.name)
        await self.session.flush()
        return p

    async def update(self, project_uuid: UUID, meta: dict[str, Any], *, actor: UUID) -> Project:
        p = await self.get(project_uuid)
        if "name" in meta and meta["name"] != p.name:
            p.name = await self._check_name(p.team_uuid, meta["name"], exclude=p.uuid)
        if "description" in meta:
            p.description = (meta["description"] or "").strip()[:500] or None
        if "archived" in meta:
            p.status = "archived" if meta["archived"] else "active"
        p.version += 1
        p.updated_by = actor
        record(self.session, actor, "project.update", "project", p.uuid, p.name)
        await self.session.flush()
        return p

    # ───────────────────────────── 绑定模板

    async def bindings(self, project_uuid: UUID) -> list[ProjectTemplate]:
        return list(
            await self.session.scalars(
                select(ProjectTemplate)
                .where(ProjectTemplate.project_uuid == project_uuid)
                .order_by(ProjectTemplate.id)
            )
        )

    async def binding(self, project_uuid: UUID, template_uuid: UUID) -> ProjectTemplate:
        b = await self.session.scalar(
            select(ProjectTemplate).where(
                ProjectTemplate.project_uuid == project_uuid,
                ProjectTemplate.template_uuid == template_uuid,
            )
        )
        if b is None:
            raise NotFound("BINDING_NOT_FOUND", "项目没有绑定这个模板")
        return b

    async def bind(
        self, project_uuid: UUID, template_uuid: UUID, *, actor: UUID
    ) -> ProjectTemplate:
        """只能绑本团队已经发布过的模板。"""
        p = await self.get(project_uuid)
        t = await self.templates.get(template_uuid)
        if t.team_uuid != p.team_uuid:
            raise Invalid("TEMPLATE_OTHER_TEAM", "只能绑定本团队的模板")
        if t.latest_version_no == 0:
            raise Invalid("TEMPLATE_NOT_PUBLISHED", "模板还没发布过，发布后才能绑定")
        existing = await self.session.scalar(
            select(ProjectTemplate).where(
                ProjectTemplate.project_uuid == p.uuid, ProjectTemplate.template_uuid == t.uuid
            )
        )
        if existing:
            return existing
        b = ProjectTemplate(project_uuid=p.uuid, template_uuid=t.uuid)
        self.session.add(b)
        record(self.session, actor, "project.bind", "project", p.uuid, p.name, template=t.name)
        await self.session.flush()
        return b

    async def unbind(self, project_uuid: UUID, template_uuid: UUID, *, actor: UUID) -> None:
        """解绑不影响进行中的流程（它们锁在发起时的版本上）。"""
        p = await self.get(project_uuid)
        b = await self.binding(p.uuid, template_uuid)
        await self.session.delete(b)
        record(
            self.session,
            actor,
            "project.unbind",
            "project",
            p.uuid,
            p.name,
            template=str(template_uuid),
        )
        await self.session.flush()

    # ───────────────────────────── 分配角色

    async def assignments(self, binding: ProjectTemplate) -> list[ProjectRole]:
        return list(
            await self.session.scalars(
                select(ProjectRole)
                .where(ProjectRole.binding_uuid == binding.uuid)
                .order_by(ProjectRole.role_key, ProjectRole.sort, ProjectRole.id)
            )
        )

    async def assign(
        self,
        project_uuid: UUID,
        template_uuid: UUID,
        role_key: str,
        members: list[UUID],
        agent: UUID | None,
        *,
        actor: UUID,
    ) -> list[str]:
        """整体替换一个角色的分配。返回提示（如授使用权失败）。

        成员只能选本团队读写以上的人，数字员工只能选本团队接入的、能用的。只影响还没开工的节点
        （开工时锁数字员工）；执行人不锁，换人立刻生效。
        """
        p = await self.get(project_uuid)
        b = await self.binding(p.uuid, template_uuid)
        v = await self.templates.latest_published(template_uuid)
        if v is None or role_key not in (v.structure.get("roles") or {}):
            raise Invalid("ROLE_NOT_IN_TEMPLATE", "模板最新发布版里没有这个角色")
        writers = {m.uid for m in await self.teams.members(p.team_uuid, "WRITE")}
        bad = [str(u) for u in members if u not in writers]
        if bad:
            raise Invalid("NOT_TEAM_WRITER", "只能选本团队读写以上的成员", users=bad)
        if agent is not None:
            ta = await self.teams.agent(p.team_uuid, agent)
            if not ta.available:
                raise Invalid("AGENT_UNAVAILABLE", f"数字员工「{ta.name}」现在不能用")
        await self.session.execute(
            delete(ProjectRole).where(
                ProjectRole.binding_uuid == b.uuid, ProjectRole.role_key == role_key
            )
        )
        for i, uid in enumerate(dict.fromkeys(members)):
            self.session.add(
                ProjectRole(
                    binding_uuid=b.uuid,
                    role_key=role_key,
                    assignee_type="user",
                    assignee_uuid=uid,
                    sort=i,
                )
            )
        if agent is not None:
            self.session.add(
                ProjectRole(
                    binding_uuid=b.uuid,
                    role_key=role_key,
                    assignee_type="agent",
                    assignee_uuid=agent,
                )
            )
        record(
            self.session,
            actor,
            "project.assign",
            "project",
            p.uuid,
            p.name,
            template=str(template_uuid),
            role=role_key,
            members=len(members),
            agent=agent,
        )
        await self.session.flush()
        # 给新加的人、新换的数字员工补授使用权（已经有的跳过）
        try:
            await self.grant_use(p.uuid, template_uuid)
        except TFError as exc:
            log.warning("分配角色后授使用权失败：%s", exc.message)
            return [
                f"给数字员工授使用权失败：{exc.message}（请让接入人确认他还是数字员工的 Owner）"
            ]
        return []

    # ───────────────────────────── 配齐与解析

    async def _role_rows(
        self, binding: ProjectTemplate
    ) -> tuple[dict[str, list[UUID]], dict[str, UUID]]:
        users: dict[str, list[UUID]] = {}
        agents: dict[str, UUID] = {}
        for r in await self.assignments(binding):
            if r.assignee_type == "user":
                users.setdefault(r.role_key, []).append(r.assignee_uuid)
            else:
                agents[r.role_key] = r.assignee_uuid
        return users, agents

    async def resolve(
        self, project_uuid: UUID, template_uuid: UUID, *, roles_of: dict[str, Any] | None = None
    ) -> RoleTable:
        """现在谁是这些角色。roles_of 给了就按它（流程锁定的版本）的角色表，否则按最新发布版。"""
        p = await self.get(project_uuid)
        b = await self.binding(p.uuid, template_uuid)
        if roles_of is None:
            v = await self.templates.latest_published(template_uuid)
            roles_of = v.structure if v else {"roles": {}}
            version_no = v.version_no if v else 0
        else:
            version_no = 0
        users, agents = await self._role_rows(b)
        writers = {m.uid for m in await self.teams.members(p.team_uuid, "WRITE")}
        team_agents = {ta.uuid: ta for ta in await self.teams.agents(p.team_uuid)}
        members: dict[str, list[UUID]] = {}
        agent_of: dict[str, tuple[UUID, str] | None] = {}
        for key in roles_of.get("roles") or {}:
            members[key] = [u for u in users.get(key, []) if u in writers]
            ta = team_agents.get(agents[key]) if key in agents else None
            agent_of[key] = (
                (ta.uuid, ta.atlas_agent_id) if ta is not None and ta.available else None
            )
        return RoleTable(version_no=version_no, members=members, agents=agent_of)

    async def readiness(self, project_uuid: UUID) -> dict[UUID, Readiness]:
        p = await self.get(project_uuid)
        out: dict[UUID, Readiness] = {}
        for b in await self.bindings(p.uuid):
            v = await self.templates.latest_published(b.template_uuid)
            if v is None:
                out[b.template_uuid] = Readiness(b.template_uuid, 0, [("", "not_published")])
                continue
            table = await self.resolve(p.uuid, b.template_uuid, roles_of=v.structure)
            missing: list[tuple[str, str]] = []
            exec_roles = structure.exec_roles(v.structure)
            for key in [*exec_roles, *structure.review_roles(v.structure)]:
                if any(k == key for k, _ in missing):
                    continue
                if not table.members.get(key):
                    missing.append((key, "no_member"))
                elif key in exec_roles and table.agents.get(key) is None:
                    missing.append((key, "no_agent"))
            out[b.template_uuid] = Readiness(b.template_uuid, v.version_no, missing)
        return out

    async def launchable(self, project_uuid: UUID) -> list[UUID]:
        """能用来发起的模板：配齐了，团队和项目都没归档、模板没归档。"""
        p = await self.get(project_uuid)
        if p.status != "active" or not await self.teams.accepting(p.team_uuid):
            return []
        ready = await self.readiness(p.uuid)
        out = []
        for tid, r in ready.items():
            if r.ok and (await self.templates.get(tid)).status == "active":
                out.append(tid)
        return out

    async def grant_use(
        self, project_uuid: UUID, template_uuid: UUID, *, roles_of: dict[str, Any] | None = None
    ) -> None:
        """这个模板各角色当前的成员 × 各执行角色配的数字员工 → 授「使用」。"""
        p = await self.get(project_uuid)
        table = await self.resolve(p.uuid, template_uuid, roles_of=roles_of)
        agents = [a[0] for a in table.agents.values() if a is not None]
        await self.teams.grant_use(p.team_uuid, list(dict.fromkeys(agents)), table.people())
