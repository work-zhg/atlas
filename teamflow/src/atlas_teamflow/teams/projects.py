"""项目与角色分配（团队设计 §08）。

- 项目绑定模板 id，跟随模板当前版本；只能绑定已发布、未停用的模板；更换只影响之后新发起的流程。
- 角色分配按「角色 → 多个被分配者」存行；保存某个角色时整体替换该角色的行，并递增项目 row_version。
- 约束（执行角色 ≥1 人 + 恰好 1 个可用 Agent；评审角色 ≥1 人）不满足只标红、不阻止；
  明确不合法的（非团队成员、评审角色分配 Agent、执行角色多个 Agent）直接拒绝。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

from ..audit import record
from ..db.models import (
    FlowTemplate,
    FlowTemplateVersion,
    Project,
    RoleAssignment,
    TeamAgent,
    TfUser,
)
from ..errors import Conflict, Invalid, NotFound
from ..templates import structure
from .service import TeamService, _text

__all__ = ["ProjectService"]


class ProjectService:
    def __init__(self, teams: TeamService) -> None:
        self.teams = teams
        self.session = teams.session
        self.p = teams.p

    # ───────────────────────────── 读取

    async def get(self, uuid: UUID) -> Project:
        pr = (
            await self.session.execute(select(Project).where(Project.uuid == uuid))
        ).scalar_one_or_none()
        if pr is None:
            raise NotFound("PROJECT_NOT_FOUND", "项目不存在")
        await self.teams.visible(pr.team_uuid)
        return pr

    async def list(self, team_uuid: UUID, *, include_archived: bool = True) -> list[Project]:
        await self.teams.visible(team_uuid)
        stmt = (
            select(Project)
            .where(Project.team_uuid == team_uuid)
            .order_by(Project.status, Project.id)
        )
        if not include_archived:
            stmt = stmt.where(Project.status == "active")
        return list(await self.session.scalars(stmt))

    async def template_of(
        self, pr: Project
    ) -> tuple[FlowTemplate | None, FlowTemplateVersion | None]:
        if pr.flow_template_uuid is None:
            return None, None
        t = (
            await self.session.execute(
                select(FlowTemplate).where(FlowTemplate.uuid == pr.flow_template_uuid)
            )
        ).scalar_one()
        v = None
        if t.current_version_uuid:
            v = (
                await self.session.execute(
                    select(FlowTemplateVersion).where(
                        FlowTemplateVersion.uuid == t.current_version_uuid
                    )
                )
            ).scalar_one()
        return t, v

    async def _bindable(self, template_uuid: UUID | None) -> UUID:
        if template_uuid is None:
            raise Invalid("VALIDATION_FAILED", "请选择流程模板", field="flow_template_id")
        t = (
            await self.session.execute(
                select(FlowTemplate).where(FlowTemplate.uuid == template_uuid)
            )
        ).scalar_one_or_none()
        if t is None or t.current_version_uuid is None:
            raise Invalid(
                "TEMPLATE_NOT_PUBLISHED", "只能绑定已发布的流程模板", field="flow_template_id"
            )
        if t.status != "active":
            raise Invalid("TEMPLATE_DISABLED", "流程模板已停用，不能绑定", field="flow_template_id")
        return t.uuid

    async def _check_name(self, team_uuid: UUID, name: str, exclude: UUID | None = None) -> None:
        stmt = select(Project.uuid).where(Project.team_uuid == team_uuid, Project.name == name)
        if exclude:
            stmt = stmt.where(Project.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "团队内已有同名项目")

    # ───────────────────────────── 写入

    async def create(self, team_uuid: UUID, data: dict[str, Any]) -> Project:
        t = await self.teams.visible(team_uuid)
        await self.teams._require_manage(t, "project:create")
        if t.status != "active":
            raise Conflict("TEAM_ARCHIVED", "团队已归档")
        name = _text(data.get("name"), "name", "项目名称", 64)
        await self._check_name(t.uuid, name)  # type: ignore[arg-type]
        tpl = data.get("flow_template_id")
        pr = Project(
            team_uuid=t.uuid,
            name=name,
            description=_text(data.get("description"), "description", "说明", 500, required=False),
            flow_template_uuid=await self._bindable(UUID(str(tpl)) if tpl else None),
            created_by=self.p.uuid,
            updated_by=self.p.uuid,
        )
        self.session.add(pr)
        await self.session.flush()
        record(
            self.session,
            self.p.uuid,
            "project.created",
            "project",
            pr.uuid,
            pr.name,
            team=t.name,
            template=pr.flow_template_uuid,
        )
        return pr

    async def _editable(self, uuid: UUID, version: int | None) -> Project:
        pr = await self.get(uuid)
        await self.teams._require_manage(await self.teams.get(pr.team_uuid), "project:configure")
        if version is not None and version != pr.version:
            raise Conflict("STALE_ROW_VERSION", "项目已被他人修改，请刷新后重试")
        return pr

    async def update(self, uuid: UUID, data: dict[str, Any], version: int | None) -> Project:
        pr = await self._editable(uuid, version)
        if pr.status == "archived":
            raise Conflict("PROJECT_ARCHIVED", "项目已归档，取消归档后再修改")
        before = pr.flow_template_uuid
        if "name" in data:
            name = _text(data["name"], "name", "项目名称", 64)
            await self._check_name(pr.team_uuid, name, exclude=pr.uuid)  # type: ignore[arg-type]
            pr.name = name  # type: ignore[assignment]
        if "description" in data:
            pr.description = _text(data["description"], "description", "说明", 500, required=False)
        if "flow_template_id" in data:
            new = UUID(str(data["flow_template_id"])) if data["flow_template_id"] else None
            if new != pr.flow_template_uuid:
                pr.flow_template_uuid = await self._bindable(new)
        pr.version += 1
        pr.updated_by = self.p.uuid
        record(
            self.session, self.p.uuid, "project.updated", "project", pr.uuid, pr.name,
            template_before=before if before != pr.flow_template_uuid else None,
            template_after=pr.flow_template_uuid if before != pr.flow_template_uuid else None,
        )  # fmt: skip
        await self.session.flush()
        return pr

    async def set_status(self, uuid: UUID, archived: bool) -> Project:
        pr = await self.get(uuid)
        await self.teams._require_manage(await self.teams.get(pr.team_uuid), "project:archive")
        status = "archived" if archived else "active"
        if pr.status != status:
            pr.status = status
            pr.version += 1
            record(
                self.session,
                self.p.uuid,
                f"project.{status if archived else 'unarchived'}",
                "project",
                pr.uuid,
                pr.name,
            )
            await self.session.flush()
        return pr

    # ───────────────────────────── 角色分配

    async def _assignments(self, project_uuid: UUID) -> dict[str, list[RoleAssignment]]:
        out: dict[str, list[RoleAssignment]] = defaultdict(list)
        for a in await self.session.scalars(
            select(RoleAssignment)
            .where(RoleAssignment.project_uuid == project_uuid)
            .order_by(RoleAssignment.sort, RoleAssignment.id)
        ):
            out[a.role_name].append(a)
        return out

    async def roles(self, uuid: UUID) -> dict[str, Any]:
        """模板当前版本的角色 + 分配 + 约束检查 + 涉及节点与规则。"""
        pr = await self.get(uuid)
        tpl, ver = await self.template_of(pr)
        if ver is None:
            return {"template": None, "roles": [], "incomplete": 0}
        assigned = await self._assignments(pr.uuid)
        members = await self.teams.member_ids(pr.team_uuid)
        agents = {
            a.uuid: a
            for a in await self.session.scalars(
                select(TeamAgent).where(TeamAgent.team_uuid == pr.team_uuid)
            )
        }
        user_ids = {
            a.assignee_uuid for rows in assigned.values() for a in rows if a.assignee_type == "user"
        }
        users = (
            {
                u.uuid: u
                for u in await self.session.scalars(select(TfUser).where(TfUser.uuid.in_(user_ids)))
            }
            if user_ids
            else {}
        )
        usage = _role_usage(ver.definition)
        out = []
        incomplete = 0
        for kind in ("exec", "review"):
            for role in ver.roles.get(kind, []):
                rows = assigned.get(role, [])
                people, bots = [], []
                for a in rows:
                    if a.assignee_type == "user":
                        u = users.get(a.assignee_uuid)
                        people.append(
                            {
                                "id": str(a.assignee_uuid),
                                "name": u.name if u else "未知用户",
                                "account": u.account if u else None,
                                "is_member": a.assignee_uuid in members,
                            }
                        )
                    else:
                        ag = agents.get(a.assignee_uuid)
                        bots.append(
                            {
                                "id": str(a.assignee_uuid),
                                "name": ag.name if ag else "已移出的 Agent",
                                "available": bool(ag and ag.available),
                            }
                        )
                problems = _problems(kind, people, bots)
                incomplete += bool(problems)
                out.append(
                    {
                        "name": role,
                        "kind": kind,
                        "users": people,
                        "agents": bots,
                        "problems": problems,
                        "nodes": usage.get((kind, role), []),
                    }
                )
        return {
            "template": {
                "id": str(tpl.uuid),
                "name": tpl.name,
                "status": tpl.status,
                "version": f"v{ver.major}.{ver.minor}",
            },  # type: ignore[union-attr]
            "roles": out,
            "incomplete": incomplete,
        }

    async def set_role(
        self, uuid: UUID, role: str, users: list[str], agents: list[str], version: int | None
    ) -> Project:
        pr = await self._editable(uuid, version)
        if pr.status == "archived":
            raise Conflict("PROJECT_ARCHIVED", "归档项目不能修改分配")
        _, ver = await self.template_of(pr)
        if ver is None:
            raise Invalid("TEMPLATE_NOT_PUBLISHED", "项目还没有绑定已发布的模板")
        kind = (
            "exec"
            if role in ver.roles.get("exec", [])
            else "review"
            if role in ver.roles.get("review", [])
            else None
        )
        if kind is None:
            raise Invalid("ROLE_NOT_IN_TEMPLATE", f"模板当前版本中没有角色「{role}」")
        user_ids = list(dict.fromkeys(UUID(u) for u in users))
        agent_ids = list(dict.fromkeys(UUID(a) for a in agents))
        if kind == "review" and agent_ids:
            raise Invalid("AGENT_NOT_ALLOWED", "评审角色只能分配人")
        if len(agent_ids) > 1:
            raise Invalid("TOO_MANY_AGENTS", "执行角色只能分配 1 个 Agent")
        members = await self.teams.member_ids(pr.team_uuid)
        outsiders = [str(u) for u in user_ids if u not in members]
        if outsiders:
            raise Invalid("NOT_TEAM_MEMBER", "只能分配团队成员", users=outsiders)
        if agent_ids:
            ok = await self.session.scalar(
                select(func.count())
                .select_from(TeamAgent)
                .where(TeamAgent.team_uuid == pr.team_uuid, TeamAgent.uuid.in_(agent_ids))
            )
            if ok != len(agent_ids):
                raise Invalid("NOT_TEAM_AGENT", "只能分配本团队的 Agent")
        await self._ensure_users(user_ids)
        await self.session.execute(
            delete(RoleAssignment).where(
                RoleAssignment.project_uuid == pr.uuid, RoleAssignment.role_name == role
            )
        )
        for i, u in enumerate(user_ids):
            self.session.add(
                RoleAssignment(
                    project_uuid=pr.uuid,
                    role_name=role,
                    assignee_type="user",
                    assignee_uuid=u,
                    sort=i,
                )
            )
        for a in agent_ids:
            self.session.add(
                RoleAssignment(
                    project_uuid=pr.uuid,
                    role_name=role,
                    assignee_type="agent",
                    assignee_uuid=a,
                    sort=0,
                )
            )
        pr.version += 1
        pr.updated_by = self.p.uuid
        record(
            self.session,
            self.p.uuid,
            "project.role_assigned",
            "project",
            pr.uuid,
            pr.name,
            role=role,
            users=len(user_ids),
            agents=len(agent_ids),
        )
        await self.session.flush()
        return pr

    async def _ensure_users(self, ids: list[UUID]) -> None:
        """被分配的人在本地缓存里要有名字（他们可能还没登录过 TeamFlow）。"""
        known = (
            set(await self.session.scalars(select(TfUser.uuid).where(TfUser.uuid.in_(ids))))
            if ids
            else set()
        )
        for uid in ids:
            if uid not in known:
                info = await self.teams.uc.user(uid)
                self.session.add(
                    TfUser(
                        uuid=uid,
                        account=info["account"],
                        name=info["name"],
                        email=info.get("email"),
                        status=info.get("status", "active"),
                    )
                )
        await self.session.flush()

    async def prune_non_members(self, team_uuid: UUID) -> list[dict[str, Any]]:
        """成员移除后：连带移除不再是成员的人在本团队项目中的分配，返回受影响的项目角色。"""
        members = await self.teams.member_ids(team_uuid)
        rows = (
            await self.session.execute(
                select(RoleAssignment, Project.name)
                .join(Project, Project.uuid == RoleAssignment.project_uuid)
                .where(Project.team_uuid == team_uuid, RoleAssignment.assignee_type == "user")
            )
        ).all()
        affected = []
        for a, pname in rows:
            if a.assignee_uuid not in members:
                affected.append(
                    {"project_name": pname, "role": a.role_name, "user_id": str(a.assignee_uuid)}
                )
                await self.session.delete(a)
        await self.session.flush()
        return affected

    async def completion(self, pr: Project) -> dict[str, int]:
        """项目卡片：角色分配完成度（不查用户中心，只按人数 / Agent 数粗算）。"""
        _, ver = await self.template_of(pr)
        if ver is None:
            return {"total": 0, "done": 0}
        assigned = await self._assignments(pr.uuid)
        agents = {
            a.uuid: a
            for a in await self.session.scalars(
                select(TeamAgent).where(TeamAgent.team_uuid == pr.team_uuid)
            )
        }
        total = done = 0
        for kind in ("exec", "review"):
            for role in ver.roles.get(kind, []):
                total += 1
                rows = assigned.get(role, [])
                people = [{"is_member": True} for a in rows if a.assignee_type == "user"]
                bots = [
                    {
                        "available": bool(
                            agents.get(a.assignee_uuid) and agents[a.assignee_uuid].available
                        )
                    }
                    for a in rows
                    if a.assignee_type == "agent"
                ]
                done += not _problems(kind, people, bots)
        return {"total": total, "done": done}


def _problems(kind: str, people: list[dict[str, Any]], bots: list[dict[str, Any]]) -> list[str]:
    out = []
    if not people:
        out.append("缺少人" if kind == "exec" else "未分配")
    elif any(not p["is_member"] for p in people):
        out.append("有人已不是团队成员")
    if kind == "exec":
        if not bots:
            out.append("缺少 Agent")
        elif len(bots) > 1:
            out.append("只能有 1 个 Agent")
        elif not bots[0]["available"]:
            out.append("Agent 不可用")
    return out


def _role_usage(definition: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """角色涉及的节点与评审规则（分配页帮助判断要不要多分配人）。"""
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    nodes = definition.get("nodes") or {}
    for nid in structure.flow_node_ids(definition.get("flow") or []):
        n = nodes.get(nid) or {}
        name = n.get("name") or nid
        if n.get("exec_role"):
            out[("exec", n["exec_role"])].append({"node": name, "as": "执行"})
        for key, label in (("admit_review", "准入评审"), ("exit_review", "准出评审")):
            r = n.get(key)
            if r and r.get("role"):
                out[("review", r["role"])].append(
                    {"node": name, "as": label, "rule": r.get("rule")}
                )
    return out
