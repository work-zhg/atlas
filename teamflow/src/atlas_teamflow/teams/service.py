"""团队（团队设计、权限设计 §07）：基本信息、成员（用户中心 Team 数据权限）、团队 Agent。

★ 成员与团队管理员只存用户中心：成员 = Team 读写，管理员 = Team Owner。
  本服务对成员的修改都以「当前用户」为 operator 调用户中心，Owner 规则由用户中心兜底
  （平台管理员靠 team:manage_all 成为数据管理员）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..atlas import AtlasClient
from ..audit import record
from ..db.models import Project, RoleAssignment, Team, TeamAgent
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..identity import LEVEL_RANK, Principal, TeamLevelCache
from ..process.bus import coordinator
from ..uc import UCClient

__all__ = ["TEAM_ADMIN_ROLE", "TeamService"]

#: 被指定为团队管理员的人应当具备的角色（缺少时只提示，不阻止）
TEAM_ADMIN_ROLE = "TF_TEAM_ADMIN"


def _text(
    value: str | None, field: str, label: str, max_len: int, required: bool = True
) -> str | None:
    v = (value or "").strip()
    if not v:
        if required:
            raise Invalid("VALIDATION_FAILED", f"请填写{label}", field=field)
        return None
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符", field=field)
    return v


def _grant(permission: str, item: dict[str, Any]) -> dict[str, Any]:
    stype = item.get("type", "USER")
    if stype not in ("USER", "DEPT"):
        raise Invalid("VALIDATION_FAILED", "成员类型只能是 USER 或 DEPT")
    return {
        "permission": permission,
        "subject_type": stype,
        "subject_id": str(item["id"]),
        "include_sub": bool(item.get("include_sub")) if stype == "DEPT" else False,
    }


class TeamService:
    def __init__(
        self,
        session: AsyncSession,
        principal: Principal,
        uc: UCClient,
        levels: TeamLevelCache,
        atlas: AtlasClient | None = None,
    ) -> None:
        self.session = session
        self.p = principal
        self.uc = uc
        self.levels = levels
        self.atlas = atlas

    # ───────────────────────────── 读取与级别

    async def get(self, uuid: UUID) -> Team:
        t = (await self.session.execute(select(Team).where(Team.uuid == uuid))).scalar_one_or_none()
        if t is None:
            raise NotFound("TEAM_NOT_FOUND", "团队不存在")
        return t

    async def level(self, team_uuid: UUID) -> str:
        return await self.levels.level(self.uc, team_uuid, self.p.uuid)

    async def can_manage(self, team_uuid: UUID, op: str) -> bool:
        """团队管理动作 = 操作码 ∧ 团队 Owner（权限设计 §08）。

        ★ 平台管理员（team:manage_all）只额外拥有「看全部团队、调整任意团队的成员与管理员」，
          不因此能编辑团队、配置项目。
        """
        if op == "team:member" and self.p.has("team:manage_all"):
            return True
        if not self.p.has(op):
            return False
        return LEVEL_RANK[await self.level(team_uuid)] >= LEVEL_RANK["OWNER"]

    async def visible(self, team_uuid: UUID) -> Team:
        """可见 = 团队读写以上，或 team:manage_all；看不见一律按不存在处理。"""
        t = await self.get(team_uuid)
        if self.p.has("team:manage_all"):
            return t
        if LEVEL_RANK[await self.level(team_uuid)] < LEVEL_RANK["WRITE"]:
            raise NotFound("TEAM_NOT_FOUND", "团队不存在")
        return t

    async def list(self, *, include_archived: bool = False) -> list[tuple[Team, str]]:
        stmt = select(Team).order_by(Team.id)
        if not include_archived:
            stmt = stmt.where(Team.status == "active")
        teams = list(await self.session.scalars(stmt))
        accessible = await self.uc.accessible_teams(self.p.uuid, "WRITE")
        if self.p.has("team:manage_all"):
            return [(t, accessible.get(str(t.uuid), "NONE")) for t in teams]
        return [(t, accessible[str(t.uuid)]) for t in teams if str(t.uuid) in accessible]

    async def _check_name(self, name: str, exclude: UUID | None = None) -> None:
        stmt = select(Team.uuid).where(Team.name == name)
        if exclude:
            stmt = stmt.where(Team.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "团队名称已存在")

    async def admin_role_warnings(self, user_ids: list[UUID]) -> list[str]:
        out = []
        for uid in user_ids:
            snap = await self.uc.authz(uid)
            if TEAM_ADMIN_ROLE not in snap.get("roles", []):
                out.append(
                    f"{snap['user']['name']} 没有「团队管理员」角色，"
                    "将无法新建 / 配置项目（可在用户中心授权）"
                )
        return out

    # ───────────────────────────── 基本信息

    async def create(self, data: dict[str, Any]) -> tuple[Team, list[str]]:
        self.p.require("team:create")
        name = _text(data.get("name"), "name", "团队名称", 64)
        await self._check_name(name)  # type: ignore[arg-type]
        admins = [UUID(str(a)) for a in data.get("admins") or []]
        if not admins:
            raise Invalid("VALIDATION_FAILED", "至少指定 1 名团队管理员", field="admins")
        members = data.get("members") or []
        t = Team(
            name=name,
            description=_text(data.get("description"), "description", "说明", 500, required=False),
            created_by=self.p.uuid,
            updated_by=self.p.uuid,
        )
        self.session.add(t)
        await self.session.flush()
        grants = [_grant("OWNER", {"type": "USER", "id": a}) for a in dict.fromkeys(admins)]
        grants += [
            _grant("WRITE", m)
            for m in members
            if not (m.get("type", "USER") == "USER" and UUID(str(m["id"])) in admins)
        ]
        # ★ 先写本地（未提交）再写用户中心：用户中心失败则本地随请求回滚；
        #   用户中心成功而本地提交失败的概率很低，残留授权指向不存在的团队，不影响可见性。
        await self.uc.write_team_grants(t.uuid, t.name, grants, operator=None)
        record(
            self.session,
            self.p.uuid,
            "team.create",
            "team",
            t.uuid,
            t.name,
            admins=admins,
            members=len(members),
        )
        await self.session.flush()
        return t, await self.admin_role_warnings(admins)

    async def update(self, uuid: UUID, data: dict[str, Any], version: int | None) -> Team:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:edit")
        if version is not None and version != t.version:
            raise Conflict("STALE_ROW_VERSION", "团队已被他人修改，请刷新后重试")
        renamed = False
        if "name" in data:
            name = _text(data["name"], "name", "团队名称", 64)
            if name != t.name:
                await self._check_name(name, exclude=t.uuid)  # type: ignore[arg-type]
                t.name, renamed = name, True  # type: ignore[assignment]
        if "description" in data:
            t.description = _text(data["description"], "description", "说明", 500, required=False)
        t.version += 1
        t.updated_by = self.p.uuid
        if renamed:
            await self.uc.rename_team(t.uuid, t.name)
        record(self.session, self.p.uuid, "team.update", "team", t.uuid, t.name)
        await self.session.flush()
        return t

    async def set_status(self, uuid: UUID, archived: bool) -> Team:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:edit")
        status = "archived" if archived else "active"
        if t.status != status:
            t.status = status
            t.version += 1
            record(
                self.session,
                self.p.uuid,
                f"team.{'archive' if archived else 'unarchive'}",
                "team",
                t.uuid,
                t.name,
            )
            await self.session.flush()
        return t

    async def _require_manage(self, t: Team, op: str) -> None:
        if await self.can_manage(t.uuid, op):
            return
        self.p.require(op)
        raise Forbidden("TEAM_OWNER_REQUIRED", "只有团队管理员可以执行该操作")

    # ───────────────────────────── 成员

    async def members(self, uuid: UUID) -> dict[str, Any]:
        t = await self.visible(uuid)
        acl = await self.uc.team_acl(t.uuid)
        holders = await self.uc.team_holders(t.uuid, "WRITE")
        return {
            "grants": acl,
            "holders": holders,
            "can_manage": await self.can_manage(t.uuid, "team:member"),
        }

    async def add_members(
        self, uuid: UUID, items: list[dict[str, Any]], *, admin: bool = False
    ) -> list[str]:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:member")
        if not items:
            raise Invalid("VALIDATION_FAILED", "请选择成员")
        perm = "OWNER" if admin else "WRITE"
        await self.uc.write_team_grants(
            t.uuid, t.name, [_grant(perm, i) for i in items], operator=self.p.uuid
        )
        self.levels.invalidate(t.uuid)
        coordinator().team_changed(t.uuid)  # 其他实例的缓存
        record(
            self.session,
            self.p.uuid,
            "team.member_add",
            "team",
            t.uuid,
            t.name,
            count=len(items),
            admin=admin,
        )
        users = [UUID(str(i["id"])) for i in items if i.get("type", "USER") == "USER"]
        return await self.admin_role_warnings(users) if admin else []

    async def _grant_of(self, t: Team, acl_id: str) -> dict[str, Any]:
        for g in await self.uc.team_acl(t.uuid):
            if g["id"] == acl_id:
                return g
        raise NotFound("GRANT_NOT_FOUND", "成员授权不存在")

    async def set_admin(self, uuid: UUID, acl_id: str, admin: bool) -> list[str]:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:member")
        g = await self._grant_of(t, acl_id)
        await self.uc.update_team_grant(
            acl_id, self.p.uuid, permission="OWNER" if admin else "WRITE"
        )
        self.levels.invalidate(t.uuid)
        coordinator().team_changed(t.uuid)  # 其他实例的缓存
        record(
            self.session,
            self.p.uuid,
            "team.admin_set" if admin else "team.admin_unset",
            "team",
            t.uuid,
            t.name,
            subject=g["subject"]["name"],
        )
        if admin and g["subject"]["type"] == "USER":
            return await self.admin_role_warnings([UUID(g["subject"]["id"])])
        return []

    async def remove_member(self, uuid: UUID, acl_id: str) -> None:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:member")
        g = await self._grant_of(t, acl_id)
        await self.uc.delete_team_grant(acl_id, self.p.uuid)
        self.levels.invalidate(t.uuid)
        coordinator().team_changed(t.uuid)  # 其他实例的缓存
        record(
            self.session,
            self.p.uuid,
            "team.member_remove",
            "team",
            t.uuid,
            t.name,
            subject=g["subject"]["name"],
        )

    async def member_ids(self, team_uuid: UUID) -> set[UUID]:
        return {UUID(h["user"]["id"]) for h in await self.uc.team_holders(team_uuid, "WRITE")}

    # ───────────────────────────── 团队 Agent

    async def agents(self, uuid: UUID) -> list[TeamAgent]:
        t = await self.visible(uuid)
        return list(
            await self.session.scalars(
                select(TeamAgent).where(TeamAgent.team_uuid == t.uuid).order_by(TeamAgent.id)
            )
        )

    async def agent_candidates(self, uuid: UUID, q: str | None) -> list[dict[str, Any]]:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:agent")
        assert self.atlas is not None
        added = set(
            await self.session.scalars(
                select(TeamAgent.atlas_agent_id).where(TeamAgent.team_uuid == t.uuid)
            )
        )
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "description": a.get("description"),
                "avatar_key": a.get("avatar_key"),
                "model": a.get("model"),
                "added": a["id"] in added,
            }
            for a in await self.atlas.list_agents(q=q, status="enabled")
        ]

    async def add_agent(self, uuid: UUID, atlas_agent_id: str) -> TeamAgent:
        t = await self.visible(uuid)
        await self._require_manage(t, "team:agent")
        assert self.atlas is not None
        a = await self.atlas.get_agent(atlas_agent_id)
        if a is None or a.get("status") != "enabled":
            raise Invalid("AGENT_UNAVAILABLE", "Agent 不存在或未启用")
        if await self.session.scalar(
            select(TeamAgent.uuid).where(
                TeamAgent.team_uuid == t.uuid, TeamAgent.atlas_agent_id == str(a["id"])
            )
        ):
            raise Conflict("AGENT_EXISTS", "该 Agent 已在团队中")
        ta = TeamAgent(
            team_uuid=t.uuid,
            atlas_agent_id=str(a["id"]),
            name=a["name"][:100],
            avatar_key=a.get("avatar_key"),
            description=a.get("description"),
            model=a.get("model"),
            added_by=self.p.uuid,
        )
        self.session.add(ta)
        record(self.session, self.p.uuid, "team.agent_add", "team", t.uuid, t.name, agent=ta.name)
        await self.session.flush()
        return ta

    async def agent_usage(self, team_uuid: UUID, agent_uuid: UUID) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Project.uuid, Project.name, RoleAssignment.role_name)
                .join(RoleAssignment, RoleAssignment.project_uuid == Project.uuid)
                .where(
                    Project.team_uuid == team_uuid,
                    RoleAssignment.assignee_type == "agent",
                    RoleAssignment.assignee_uuid == agent_uuid,
                )
            )
        ).all()
        return [{"project_id": str(p), "project_name": n, "role": r} for p, n, r in rows]

    async def remove_agent(
        self, uuid: UUID, agent_uuid: UUID, confirm: bool
    ) -> list[dict[str, Any]]:
        """移出 Agent：被项目角色使用时先 409 返回影响，confirm 后连同分配一起移除。"""
        t = await self.visible(uuid)
        await self._require_manage(t, "team:agent")
        ta = (
            await self.session.execute(
                select(TeamAgent).where(TeamAgent.uuid == agent_uuid, TeamAgent.team_uuid == t.uuid)
            )
        ).scalar_one_or_none()
        if ta is None:
            raise NotFound("AGENT_NOT_FOUND", "团队中没有该 Agent")
        usage = await self.agent_usage(t.uuid, ta.uuid)
        if usage and not confirm:
            raise Conflict(
                "AGENT_IN_USE", "该 Agent 正在项目角色中使用，确认后将一并移除分配", usage=usage
            )
        await self.session.execute(
            delete(RoleAssignment).where(
                RoleAssignment.assignee_type == "agent", RoleAssignment.assignee_uuid == ta.uuid
            )
        )
        await self.session.delete(ta)
        record(
            self.session,
            self.p.uuid,
            "team.agent_remove",
            "team",
            t.uuid,
            t.name,
            agent=ta.name,
            affected=len(usage),
        )
        await self.session.flush()
        return usage

    async def sync_agents(self, uuid: UUID) -> list[TeamAgent]:
        """按 Atlas 刷新快照与可用性（不存在或未启用 → 不可用）。"""
        t = await self.visible(uuid)
        assert self.atlas is not None
        rows = list(
            await self.session.scalars(select(TeamAgent).where(TeamAgent.team_uuid == t.uuid))
        )
        for ta in rows:
            a = await self.atlas.get_agent(ta.atlas_agent_id)
            ta.available = bool(a) and a.get("status") == "enabled"  # type: ignore[union-attr]
            if a:
                ta.name = a["name"][:100]
                ta.avatar_key = a.get("avatar_key")
                ta.description = a.get("description")
                ta.model = a.get("model")
            ta.synced_at = utcnow()
        await self.session.flush()
        return rows
