"""团队：列表 / 详情（成员可见）、新建（平台管理员）、编辑与成员 / Agent 管理（团队管理员）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from ..db.models import Project, RoleAssignment, TeamAgent
from ..teams.projects import ProjectService
from .deps import ProjectsDep, TeamsDep
from .views import agent_view, project_view, team_view

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


class Subject(BaseModel):
    type: str = "USER"
    id: UUID
    include_sub: bool = False


class TeamCreate(BaseModel):
    name: str
    description: str | None = None
    admins: list[UUID]
    members: list[Subject] = []


class TeamPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    version: int | None = None


class MembersAdd(BaseModel):
    subjects: list[Subject]
    admin: bool = False


class AdminSet(BaseModel):
    admin: bool


class AgentAdd(BaseModel):
    atlas_agent_id: str


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    flow_template_id: UUID | None = None


async def _card(teams: TeamsDep, team: Any, level: str) -> dict[str, Any]:
    s = teams.session
    agents = list(
        await s.scalars(
            select(TeamAgent).where(TeamAgent.team_uuid == team.uuid).order_by(TeamAgent.id)
        )
    )
    projects = int(
        await s.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.team_uuid == team.uuid, Project.status == "active")
        )
        or 0
    )
    return team_view(
        team,
        my_level=level,
        agent_count=len(agents),
        agents=[
            {"name": a.name, "avatar_key": a.avatar_key, "available": a.available}
            for a in agents[:6]
        ],
        project_count=projects,
    )


@router.get("")
async def list_teams(teams: TeamsDep, include_archived: bool = False) -> list[dict[str, Any]]:
    out = []
    for t, level in await teams.list(include_archived=include_archived):
        card = await _card(teams, t, level)
        card["member_count"] = len(await teams.uc.team_holders(t.uuid, "WRITE"))
        out.append(card)
    return out


@router.post("", status_code=201)
async def create(body: TeamCreate, teams: TeamsDep) -> dict[str, Any]:
    t, warnings = await teams.create(
        {
            **body.model_dump(exclude={"members", "admins"}),
            "admins": body.admins,
            "members": [m.model_dump() for m in body.members],
        }
    )
    return {**team_view(t), "warnings": warnings}


@router.get("/{tid}")
async def detail(tid: UUID, teams: TeamsDep, projects: ProjectsDep) -> dict[str, Any]:
    t = await teams.visible(tid)
    level = await teams.level(t.uuid)
    can = {
        op.split(":")[1]: await teams.can_manage(t.uuid, op)
        for op in ("team:edit", "team:member", "team:agent", "project:create")
    }
    members = await teams.members(t.uuid)
    agents = await teams.agents(t.uuid)
    # 成员 / Agent 在各项目担任的角色
    rows = (
        await teams.session.execute(
            select(
                RoleAssignment.assignee_type,
                RoleAssignment.assignee_uuid,
                RoleAssignment.role_name,
                Project.name,
            )
            .join(Project, Project.uuid == RoleAssignment.project_uuid)
            .where(Project.team_uuid == t.uuid, Project.status == "active")
        )
    ).all()
    roles: dict[str, list[str]] = {}
    for _, uid, role, pname in rows:
        roles.setdefault(str(uid), []).append(f"{pname} · {role}")
    for h in members["holders"]:
        h["roles"] = roles.get(h["user"]["id"], [])
    prs = []
    for pr in await projects.list(t.uuid):
        tpl, ver = await projects.template_of(pr)
        prs.append(
            project_view(
                pr,
                template_name=tpl.name if tpl else None,
                template_version=f"v{ver.major}.{ver.minor}" if ver else None,
                completion=await projects.completion(pr),
            )
        )
    return {
        **team_view(t),
        "my_level": level,
        "can": can,
        "members": members,
        "agents": [agent_view(a, roles=roles.get(str(a.uuid), [])) for a in agents],
        "projects": prs,
    }


@router.patch("/{tid}")
async def update(tid: UUID, body: TeamPatch, teams: TeamsDep) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    return team_view(await teams.update(tid, data, version))


@router.post("/{tid}/archive")
async def archive(tid: UUID, teams: TeamsDep) -> dict[str, Any]:
    return team_view(await teams.set_status(tid, True))


@router.post("/{tid}/unarchive")
async def unarchive(tid: UUID, teams: TeamsDep) -> dict[str, Any]:
    return team_view(await teams.set_status(tid, False))


# ───────────────────────────── 成员


@router.get("/{tid}/members")
async def members(tid: UUID, teams: TeamsDep) -> dict[str, Any]:
    return await teams.members(tid)


@router.post("/{tid}/members")
async def add_members(tid: UUID, body: MembersAdd, teams: TeamsDep) -> dict[str, Any]:
    warnings = await teams.add_members(
        tid, [s.model_dump() for s in body.subjects], admin=body.admin
    )
    return {"warnings": warnings}


@router.patch("/{tid}/members/{acl_id}")
async def set_admin(tid: UUID, acl_id: str, body: AdminSet, teams: TeamsDep) -> dict[str, Any]:
    return {"warnings": await teams.set_admin(tid, acl_id, body.admin)}


@router.delete("/{tid}/members/{acl_id}")
async def remove_member(tid: UUID, acl_id: str, teams: TeamsDep) -> dict[str, Any]:
    await teams.remove_member(tid, acl_id)
    # 不再是成员的人：连带移除其角色分配（同一请求事务）
    affected = await ProjectService(teams).prune_non_members(tid)
    return {"removed_assignments": affected}


# ───────────────────────────── Agent


@router.get("/{tid}/agent-candidates")
async def agent_candidates(
    tid: UUID, teams: TeamsDep, q: str | None = None
) -> list[dict[str, Any]]:
    return await teams.agent_candidates(tid, q)


@router.post("/{tid}/agents", status_code=201)
async def add_agent(tid: UUID, body: AgentAdd, teams: TeamsDep) -> dict[str, Any]:
    return agent_view(await teams.add_agent(tid, body.atlas_agent_id))


@router.delete("/{tid}/agents/{agent_id}")
async def remove_agent(
    tid: UUID, agent_id: UUID, teams: TeamsDep, confirm: bool = False
) -> dict[str, Any]:
    return {"removed_assignments": await teams.remove_agent(tid, agent_id, confirm)}


@router.post("/{tid}/agents/sync")
async def sync_agents(tid: UUID, teams: TeamsDep) -> list[dict[str, Any]]:
    return [agent_view(a) for a in await teams.sync_agents(tid)]


# ───────────────────────────── 项目


@router.post("/{tid}/projects", status_code=201)
async def create_project(tid: UUID, body: ProjectCreate, projects: ProjectsDep) -> dict[str, Any]:
    pr = await projects.create(tid, body.model_dump())
    return project_view(pr)
