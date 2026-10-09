"""项目：详情与角色（团队成员可见），设置 / 归档 / 角色分配（团队管理员）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from .deps import ProjectsDep
from .views import project_view, team_view

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    flow_template_id: UUID | None = None
    version: int | None = None


class RoleAssign(BaseModel):
    users: list[UUID] = []
    agents: list[UUID] = []
    version: int | None = None


async def _detail(projects: ProjectsDep, uuid: UUID) -> dict[str, Any]:
    pr = await projects.get(uuid)
    team = await projects.teams.get(pr.team_uuid)
    tpl, ver = await projects.template_of(pr)
    roles = await projects.roles(pr.uuid)
    can = {
        op.split(":")[1]: await projects.teams.can_manage(team.uuid, op)
        for op in ("project:configure", "project:archive")
    }
    hints = []
    if pr.status == "archived":
        hints.append({"kind": "archived", "message": "项目已归档：不能发起新流程、不能修改分配"})
    if tpl and tpl.status != "active":
        hints.append({"kind": "template_disabled", "message": "绑定的模板已停用，建议更换"})
    if roles["incomplete"]:
        hints.append({"kind": "roles", "message": f"{roles['incomplete']} 个角色未分配或不完整"})
    if any(not a["available"] for r in roles["roles"] for a in r["agents"]):
        hints.append({"kind": "agent_unavailable", "message": "有执行 Agent 不可用"})
    return {
        **project_view(
            pr,
            template_name=tpl.name if tpl else None,
            template_version=f"v{ver.major}.{ver.minor}" if ver else None,
        ),
        "team": team_view(team),
        "can": can,
        "roles": roles,
        "hints": hints,
    }


@router.get("/{pid}")
async def detail(pid: UUID, projects: ProjectsDep) -> dict[str, Any]:
    return await _detail(projects, pid)


@router.patch("/{pid}")
async def update(pid: UUID, body: ProjectPatch, projects: ProjectsDep) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    await projects.update(pid, data, version)
    return await _detail(projects, pid)


@router.post("/{pid}/archive")
async def archive(pid: UUID, projects: ProjectsDep) -> dict[str, Any]:
    await projects.set_status(pid, True)
    return await _detail(projects, pid)


@router.post("/{pid}/unarchive")
async def unarchive(pid: UUID, projects: ProjectsDep) -> dict[str, Any]:
    await projects.set_status(pid, False)
    return await _detail(projects, pid)


@router.get("/{pid}/roles")
async def roles(pid: UUID, projects: ProjectsDep) -> dict[str, Any]:
    return await projects.roles(pid)


@router.put("/{pid}/roles/{role}")
async def assign(pid: UUID, role: str, body: RoleAssign, projects: ProjectsDep) -> dict[str, Any]:
    await projects.set_role(
        pid, role, [str(u) for u in body.users], [str(a) for a in body.agents], body.version
    )
    return await _detail(projects, pid)
