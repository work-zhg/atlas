"""管理接口（/api/v1）：团队、成员与数字员工、流程模板、流程市场、项目与项目配置。

★ 每个接口先问访问控制（操作码 ∧ 团队级别；流程市场的写要「系统租户 + 流程市场读写」），再调领域。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import structure
from ..access import Action, MarketLevel
from ..errors import Conflict
from .deps import AccessDep, PrincipalDep, ServicesDep
from .views import (
    agent_out,
    card_out,
    market_out,
    project_out,
    team_out,
    template_out,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["mgmt"])


# ───────────────────────────── 团队


class Subject(BaseModel):
    type: Literal["USER", "DEPT"] = "USER"
    id: str
    include_sub: bool = False
    level: Literal["OWNER", "WRITE", "READ"] = "WRITE"


class TeamIn(BaseModel):
    name: str
    description: str | None = None
    members: list[Subject] = Field(default_factory=list)


class TeamPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    version: int | None = None


class ArchiveIn(BaseModel):
    archived: bool


class MembersIn(BaseModel):
    subjects: list[Subject]
    level: Literal["OWNER", "WRITE", "READ"] = "WRITE"


class LevelIn(BaseModel):
    level: Literal["OWNER", "WRITE", "READ"]


class AgentIn(BaseModel):
    atlas_agent_id: str


class ChannelsIn(BaseModel):
    wecom: bool = False
    dingtalk: bool = False
    email_digest: bool = True


@router.get("/teams")
async def list_teams(
    p: PrincipalDep, access: AccessDep, svc: ServicesDep, archived: bool = False
) -> list[dict[str, Any]]:
    only = await access.visible_teams(p)
    out = []
    for t in await svc.teams.list(only, include_archived=archived):
        out.append(team_out(t, await access.team_level(p, t.uuid)))
    return out


@router.post("/teams", status_code=201)
async def create_team(
    body: TeamIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.TEAM_CREATE)
    t = await svc.teams.create(
        body.name, body.description, [m.model_dump() for m in body.members], actor=p.uid
    )
    return team_out(t, "OWNER")


@router.get("/teams/{team_id}")
async def get_team(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    return team_out(await svc.teams.get(team_id), await access.team_level(p, team_id))


@router.patch("/teams/{team_id}")
async def update_team(
    team_id: UUID, body: TeamPatch, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    t = await svc.teams.update(
        team_id, name=body.name, description=body.description, version=body.version, actor=p.uid
    )
    return team_out(t, await access.team_level(p, team_id))


@router.post("/teams/{team_id}/archive")
async def archive_team(
    team_id: UUID, body: ArchiveIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    return team_out(await svc.teams.archive(team_id, body.archived, actor=p.uid))


@router.get("/teams/{team_id}/members")
async def team_members(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    holders = await svc.teams.members(team_id)
    return {
        "grants": await svc.teams.grants(team_id),
        "members": [
            {"id": str(m.uid), "name": m.name, "account": m.account, "level": m.level}
            for m in holders
        ],
    }


@router.post("/teams/{team_id}/members", status_code=201)
async def add_members(
    team_id: UUID, body: MembersIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    await svc.teams.add_members(
        team_id, [s.model_dump() for s in body.subjects], body.level, actor=p.uid
    )
    return {"ok": True}


@router.patch("/teams/{team_id}/members/{acl_id}")
async def set_member(
    team_id: UUID, acl_id: str, body: LevelIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    await svc.teams.set_member(team_id, acl_id, body.level, actor=p.uid)
    return {"ok": True}


@router.delete("/teams/{team_id}/members/{acl_id}")
async def remove_member(
    team_id: UUID, acl_id: str, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    await svc.teams.remove_member(team_id, acl_id, actor=p.uid)
    return {"ok": True}


@router.get("/teams/{team_id}/agents")
async def team_agents(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> list[dict[str, Any]]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    stats = await svc.process.agent_stats(team_id)
    return [agent_out(a, stats.get(a.atlas_agent_id)) for a in await svc.teams.agents(team_id)]


@router.get("/teams/{team_id}/agents/candidates")
async def agent_candidates(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep, q: str | None = None
) -> list[dict[str, Any]]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    return [{**card_out(c), "added": added} for c, added in await svc.teams.candidates(team_id, q)]


@router.post("/teams/{team_id}/agents", status_code=201)
async def add_agent(
    team_id: UUID, body: AgentIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    return agent_out(await svc.teams.add_agent(team_id, body.atlas_agent_id, actor=p.uid))


@router.post("/teams/{team_id}/agents/sync")
async def sync_agents(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> list[dict[str, Any]]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    return [agent_out(a) for a in await svc.teams.sync_agents(team_id)]


@router.get("/teams/{team_id}/agents/{agent_id}")
async def agent_card(
    team_id: UUID, agent_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    ta, card = await svc.teams.agent_card(team_id, agent_id)
    stats = (await svc.process.agent_stats(team_id)).get(ta.atlas_agent_id)
    return {**agent_out(ta, stats), "card": card_out(card) if card else None}


@router.delete("/teams/{team_id}/agents/{agent_id}")
async def remove_agent(
    team_id: UUID, agent_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    await svc.teams.remove_agent(team_id, agent_id, actor=p.uid)
    return {"ok": True}


@router.get("/teams/{team_id}/channels")
async def team_channels(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    c = await svc.inbox.team_channels(team_id)
    return {"wecom": c.wecom, "dingtalk": c.dingtalk, "email_digest": c.email_digest}


@router.put("/teams/{team_id}/channels")
async def set_team_channels(
    team_id: UUID, body: ChannelsIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await access.require(p, Action.TEAM_CONFIG, team_id)
    c = await svc.inbox.set_team_channels(
        team_id, wecom=body.wecom, dingtalk=body.dingtalk, email_digest=body.email_digest
    )
    return {"wecom": c.wecom, "dingtalk": c.dingtalk, "email_digest": c.email_digest}


# ───────────────────────────── 流程模板


class TemplateIn(BaseModel):
    name: str | None = None
    #: 给了就是从流程市场拷贝
    market_template_id: UUID | None = None


class TemplatePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    archived: bool | None = None


class DraftIn(BaseModel):
    structure: dict[str, Any]
    rev: int


class PublishIn(BaseModel):
    note: str | None = None


async def _template_team(svc: Any, template_id: UUID) -> UUID:
    return (await svc.templates.get(template_id)).team_uuid


@router.get("/teams/{team_id}/templates")
async def list_templates(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep, archived: bool = False
) -> list[dict[str, Any]]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    return [
        template_out(t) for t in await svc.templates.of_team(team_id, include_archived=archived)
    ]


@router.post("/teams/{team_id}/templates", status_code=201)
async def create_template(
    team_id: UUID, body: TemplateIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.FLOW_CONFIG, team_id)
    await svc.teams.get(team_id)
    if body.market_template_id:
        t = await svc.templates.copy_from_market(team_id, body.market_template_id, actor=p.uid)
    else:
        t = await svc.templates.create_blank(team_id, body.name or "", actor=p.uid)
    return template_out(t, await svc.templates.draft(t.uuid))


@router.get("/templates/{template_id}")
async def get_template(
    template_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    t = await svc.templates.get(template_id)
    await access.require(p, Action.PROCESS_VIEW, t.team_uuid)
    return template_out(t, await svc.templates.draft(t.uuid), await svc.templates.versions(t.uuid))


@router.patch("/templates/{template_id}")
async def update_template(
    template_id: UUID, body: TemplatePatch, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.FLOW_CONFIG, await _template_team(svc, template_id))
    return template_out(
        await svc.templates.update(template_id, body.model_dump(exclude_none=True), actor=p.uid)
    )


@router.put("/templates/{template_id}/draft")
async def save_draft(
    template_id: UUID, body: DraftIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.FLOW_CONFIG, await _template_team(svc, template_id))
    d = await svc.templates.save_draft(template_id, body.structure, rev=body.rev, actor=p.uid)
    errors, warnings = await svc.templates.validate(template_id)
    return {"structure": d.structure, "rev": d.rev, "errors": errors, "warnings": warnings}


@router.post("/templates/{template_id}/validate")
async def validate_template(
    template_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.PROCESS_VIEW, await _template_team(svc, template_id))
    errors, warnings = await svc.templates.validate(template_id)
    return {"errors": errors, "warnings": warnings}


@router.post("/templates/{template_id}/publish", status_code=201)
async def publish_template(
    template_id: UUID, body: PublishIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.FLOW_CONFIG, await _template_team(svc, template_id))
    v = await svc.templates.publish(template_id, body.note, actor=p.uid)
    return {"id": str(v.uuid), "version_no": v.version_no}


@router.get("/templates/{template_id}/versions/{version_no}")
async def get_version(
    template_id: UUID, version_no: int, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.PROCESS_VIEW, await _template_team(svc, template_id))
    for v in await svc.templates.versions(template_id):
        if v.version_no == version_no:
            return {
                "id": str(v.uuid),
                "version_no": v.version_no,
                "structure": v.structure,
                "note": v.note,
            }
    raise Conflict("VERSION_NOT_FOUND", "没有这个版本")


# ───────────────────────────── 流程市场


class MarketIn(BaseModel):
    name: str
    category: Literal["rd", "content", "legal", "data"]


class MarketDraftIn(BaseModel):
    structure: dict[str, Any] | None = None
    name: str | None = None
    category: Literal["rd", "content", "legal", "data"] | None = None
    description: str | None = None
    who: str | None = None
    icon: str | None = None
    color: str | None = None


class ListedIn(BaseModel):
    listed: bool


@router.get("/market")
async def market_listing(
    p: PrincipalDep, access: AccessDep, svc: ServicesDep, category: str | None = None
) -> dict[str, Any]:
    maintainer = access.market_level(p) is MarketLevel.MAINTAIN
    rows = await svc.market.listing(category, maintainer=maintainer)
    items = []
    for t in rows:
        stats = await svc.market.stats(t.uuid) if maintainer else None
        items.append(market_out(t, maintainer=False, stats=stats))
    return {"maintainer": maintainer, "items": items}


@router.get("/market/{template_id}")
async def market_detail(
    template_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    maintainer = access.market_level(p) is MarketLevel.MAINTAIN
    t, latest = await svc.market.detail(template_id, maintainer=maintainer)
    stats = await svc.market.stats(t.uuid) if maintainer else None
    return market_out(t, latest, maintainer=maintainer, stats=stats)


@router.post("/market", status_code=201)
async def market_create(
    body: MarketIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    access.require_market(p)
    return market_out(
        await svc.market.create(body.name, body.category, actor=p.uid), maintainer=True
    )


@router.put("/market/{template_id}/draft")
async def market_save(
    template_id: UUID, body: MarketDraftIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    access.require_market(p)
    meta = body.model_dump(exclude_none=True, exclude={"structure"})
    t = await svc.market.save_draft(template_id, body.structure, meta, actor=p.uid)
    errors, warnings = structure.validate(t.draft)
    return {**market_out(t, maintainer=True), "errors": errors, "warnings": warnings}


@router.post("/market/{template_id}/publish", status_code=201)
async def market_publish(
    template_id: UUID, body: PublishIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    access.require_market(p)
    v = await svc.market.publish(template_id, body.note, actor=p.uid)
    return {"id": str(v.uuid), "version": v.version}


@router.post("/market/{template_id}/listing")
async def market_listed(
    template_id: UUID, body: ListedIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    access.require_market(p)
    return market_out(
        await svc.market.set_listed(template_id, body.listed, actor=p.uid), maintainer=True
    )


# ───────────────────────────── 项目与项目配置


class ProjectIn(BaseModel):
    name: str
    description: str | None = None


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None


class BindIn(BaseModel):
    template_id: UUID


class AssignIn(BaseModel):
    members: list[UUID] = Field(default_factory=list)
    agent: UUID | None = None


@router.get("/teams/{team_id}/projects")
async def list_projects(
    team_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep, archived: bool = False
) -> list[dict[str, Any]]:
    await access.require(p, Action.PROCESS_VIEW, team_id)
    out = []
    for pr in await svc.projects.of_team(team_id, include_archived=archived):
        todos = await svc.inbox.todos(p.uid, project_uuid=pr.uuid)
        out.append(
            {
                **project_out(pr),
                "running": await svc.process.running_count(pr.uuid),
                "todos": len(todos),
            }
        )
    return out


@router.post("/teams/{team_id}/projects", status_code=201)
async def create_project(
    team_id: UUID, body: ProjectIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await access.require(p, Action.FLOW_CONFIG, team_id)
    return project_out(await svc.projects.create(team_id, body.name, body.description, actor=p.uid))


@router.get("/projects/{project_id}")
async def get_project(
    project_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.PROCESS_VIEW, pr.team_uuid)
    ready = await svc.projects.readiness(pr.uuid)
    bindings = []
    for b in await svc.projects.bindings(pr.uuid):
        t = await svc.templates.get(b.template_uuid)
        v = await svc.templates.latest_published(b.template_uuid)
        r = ready.get(b.template_uuid)
        roles: dict[str, Any] = {}
        for a in await svc.projects.assignments(b):
            slot = roles.setdefault(a.role_key, {"members": [], "agent": None})
            if a.assignee_type == "user":
                slot["members"].append(str(a.assignee_uuid))
            else:
                slot["agent"] = str(a.assignee_uuid)
        bindings.append(
            {
                "template": template_out(t),
                "version_no": v.version_no if v else 0,
                "roles_of_template": (v.structure.get("roles") if v else {}) or {},
                "exec_roles": structure.exec_roles(v.structure) if v else [],
                "assignments": roles,
                "ready": bool(r and r.ok),
                "missing": [{"role": k, "what": w} for k, w in (r.missing if r else [])],
                "running": await svc.process.running_count(pr.uuid, b.template_uuid),
            }
        )
    return {**project_out(pr), "bindings": bindings}


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: UUID, body: ProjectPatch, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.FLOW_CONFIG, pr.team_uuid)
    return project_out(
        await svc.projects.update(pr.uuid, body.model_dump(exclude_none=True), actor=p.uid)
    )


@router.post("/projects/{project_id}/templates", status_code=201)
async def bind_template(
    project_id: UUID, body: BindIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.FLOW_CONFIG, pr.team_uuid)
    await svc.projects.bind(pr.uuid, body.template_id, actor=p.uid)
    return {"ok": True}


@router.delete("/projects/{project_id}/templates/{template_id}")
async def unbind_template(
    project_id: UUID, template_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.FLOW_CONFIG, pr.team_uuid)
    running = await svc.process.running_count(pr.uuid, template_id)
    if running:
        raise Conflict("TEMPLATE_IN_USE", f"还有 {running} 条用这个模板的流程在跑，跑完再解绑")
    await svc.projects.unbind(pr.uuid, template_id, actor=p.uid)
    return {"ok": True}


@router.put("/projects/{project_id}/templates/{template_id}/roles/{role_key}")
async def assign_role(
    project_id: UUID,
    template_id: UUID,
    role_key: str,
    body: AssignIn,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.FLOW_CONFIG, pr.team_uuid)
    warnings = await svc.projects.assign(
        pr.uuid, template_id, role_key, body.members, body.agent, actor=p.uid
    )
    return {"ok": True, "warnings": warnings}


@router.get("/projects/{project_id}/launchable")
async def launchable(
    project_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> list[dict[str, Any]]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.PROCESS_VIEW, pr.team_uuid)
    return [
        template_out(await svc.templates.get(t)) for t in await svc.projects.launchable(pr.uuid)
    ]
