"""用户中心 / Atlas 的内存实现：与 uc.UCClient、atlas.AtlasClient 的方法签名一致。

规则按用户中心的真实行为简化：
- 授权快照 = 角色 → 操作码（取自 catalog），菜单从略；
- 数据权限：用户直授或所在部门授权（include_sub 按部门树判断），取最高级别；
- 数据已有授权时写入须带 operator，且其为 Owner 或数据管理员（team:manage_all）；
  至少保留一个 Owner。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from atlas_teamflow import catalog
from atlas_teamflow.atlas import AtlasEvent
from atlas_teamflow.errors import Conflict, Forbidden, Invalid, NotFound, Unauthorized

LEVELS = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}
NAMES = {v: k for k, v in LEVELS.items()}
ROLE_OPS = {r["code"]: set(r["operations"]) for r in catalog.ROLES}


class FakeUC:
    def __init__(self) -> None:
        self.users: dict[UUID, dict[str, Any]] = {}
        self.dept_map: dict[UUID, dict[str, Any]] = {}
        self.acl: list[dict[str, Any]] = []
        self.names: dict[str, str] = {}
        self.root = self.add_dept("星海科技")

    # ───────────────────────────── 构造

    def add_dept(self, name: str, parent: UUID | None = None) -> UUID:
        did = uuid.uuid4()
        self.dept_map[did] = {
            "id": str(did),
            "name": name,
            "parent_id": str(parent) if parent else None,
        }
        return did

    def add_user(self, account: str, name: str, roles: list[str], dept: UUID | None = None) -> UUID:
        uid = uuid.uuid4()
        self.users[uid] = {
            "id": str(uid),
            "account": account,
            "name": name,
            "email": f"{account}@x.com",
            "status": "active",
            "password": "pw",
            "roles": roles,
            "dept": dept or self.root,
            "access": True,
        }
        return uid

    def _ancestors(self, did: UUID) -> list[UUID]:
        out = [did]
        while self.dept_map[out[-1]]["parent_id"]:
            out.append(UUID(self.dept_map[out[-1]]["parent_id"]))
        return out

    def _snap(self, u: dict[str, Any]) -> dict[str, Any]:
        roles = sorted(u["roles"]) if u["access"] else []
        perms = sorted({op for r in roles for op in ROLE_OPS.get(r, set())})
        return {"can_access": u["access"], "roles": roles, "permissions": perms, "menus": []}

    def _user(self, uid: UUID) -> dict[str, Any]:
        u = self.users.get(uid)
        if u is None:
            raise NotFound("USER_NOT_FOUND", "用户不存在")
        return u

    @staticmethod
    def _info(u: dict[str, Any]) -> dict[str, Any]:
        return {k: u[k] for k in ("id", "account", "name", "email", "status")}

    # ───────────────────────────── 身份与授权

    async def verify_password(self, account: str, password: str) -> dict[str, Any]:
        u = next((u for u in self.users.values() if u["account"] == account), None)
        if u is None or u["password"] != password:
            raise Unauthorized("LOGIN_FAILED", "账号或密码错误")
        if u["status"] == "disabled":
            raise Forbidden("USER_DISABLED", "账号已停用")
        return {"user": self._info(u), **self._snap(u)}

    async def authz(self, user_uuid: UUID) -> dict[str, Any]:
        u = self._user(user_uuid)
        return {"user": self._info(u), **self._snap(u)}

    async def check_op(self, user_uuid: UUID, permission: str) -> bool:
        return permission in self._snap(self._user(user_uuid))["permissions"]

    async def user(self, user_uuid: UUID) -> dict[str, Any]:
        return self._info(self._user(user_uuid))

    async def search_users(self, q: str | None, *, size: int = 20) -> list[dict[str, Any]]:
        q = (q or "").lower()
        return [
            {"id": u["id"], "account": u["account"], "name": u["name"], "dept_id": str(u["dept"])}
            for u in self.users.values()
            if q in u["account"] or q in u["name"].lower()
        ][:size]

    async def depts(self) -> list[dict[str, Any]]:
        return [dict(d, leader_id=None) for d in self.dept_map.values()]

    # ───────────────────────────── 数据权限

    def _rows(self, team_uuid: UUID) -> list[dict[str, Any]]:
        return [a for a in self.acl if a["data_id"] == str(team_uuid)]

    def _hits(self, a: dict[str, Any], u: dict[str, Any]) -> bool:
        if u["status"] != "active":
            return False
        if a["subject_type"] == "USER":
            return a["subject_id"] == u["id"]
        did = UUID(a["subject_id"])
        return u["dept"] == did or (a["include_sub"] and did in self._ancestors(u["dept"]))

    def level_of(self, team_uuid: UUID, user_uuid: UUID) -> int:
        u = self._user(user_uuid)
        return max(
            (LEVELS[a["permission"]] for a in self._rows(team_uuid) if self._hits(a, u)), default=0
        )

    def _can_manage(self, team_uuid: UUID, operator: UUID) -> bool:
        return (
            self.level_of(team_uuid, operator) == 3
            or "team:manage_all" in self._snap(self._user(operator))["permissions"]
        )

    def _guard_owner(self, team_uuid: UUID) -> None:
        if not any(a["permission"] == "OWNER" for a in self._rows(team_uuid)):
            raise Conflict("LAST_OWNER", "至少保留一个 Owner")

    async def team_level(self, team_uuid: UUID, user_uuid: UUID) -> str:
        return NAMES[self.level_of(team_uuid, user_uuid)]

    async def accessible_teams(self, user_uuid: UUID, min_level: str = "WRITE") -> dict[str, str]:
        out = {}
        for tid in {a["data_id"] for a in self.acl}:
            lv = self.level_of(UUID(tid), user_uuid)
            if lv >= LEVELS[min_level]:
                out[tid] = NAMES[lv]
        return out

    async def write_team_grants(
        self, team_uuid: UUID, team_name: str, grants: list[dict[str, Any]], operator: UUID | None
    ) -> dict[str, Any]:
        rows = self._rows(team_uuid)
        if rows:
            if operator is None or not self._can_manage(team_uuid, operator):
                raise Forbidden("OWNER_REQUIRED", "只有 Owner 可以修改授权")
        elif not any(g["permission"] == "OWNER" for g in grants):
            raise Invalid("OWNER_REQUIRED", "第一次授权中必须至少有一条 OWNER")
        self.names.setdefault(str(team_uuid), team_name)
        for g in grants:
            same = next(
                (
                    a
                    for a in rows
                    if (a["subject_type"], a["subject_id"]) == (g["subject_type"], g["subject_id"])
                ),
                None,
            )
            if same:
                same["permission"], same["include_sub"] = g["permission"], g["include_sub"]
            else:
                self.acl.append({"id": str(uuid.uuid4()), "data_id": str(team_uuid), **g})
        return {"added": len(grants)}

    def _view(self, a: dict[str, Any]) -> dict[str, Any]:
        if a["subject_type"] == "USER":
            u = self.users[UUID(a["subject_id"])]
            subject = {"type": "USER", "id": u["id"], "account": u["account"], "name": u["name"]}
        else:
            subject = {
                "type": "DEPT",
                "id": a["subject_id"],
                "name": self.dept_map[UUID(a["subject_id"])]["name"],
            }
        return {
            "id": a["id"],
            "permission": a["permission"],
            "subject": subject,
            "include_sub": a["include_sub"],
        }

    async def team_acl(self, team_uuid: UUID) -> list[dict[str, Any]]:
        return [self._view(a) for a in self._rows(team_uuid)]

    async def team_holders(self, team_uuid: UUID, min_level: str = "WRITE") -> list[dict[str, Any]]:
        out = []
        for uid, u in self.users.items():
            lv = self.level_of(team_uuid, uid)
            if lv >= LEVELS[min_level]:
                out.append(
                    {
                        "user": {"id": u["id"], "account": u["account"], "name": u["name"]},
                        "permission": NAMES[lv],
                    }
                )
        return out

    def _acl(self, acl_id: str) -> dict[str, Any]:
        a = next((a for a in self.acl if a["id"] == acl_id), None)
        if a is None:
            raise NotFound("ACL_NOT_FOUND", "授权不存在")
        return a

    async def update_team_grant(
        self,
        acl_id: str,
        operator: UUID,
        *,
        permission: str | None = None,
        include_sub: bool | None = None,
    ) -> None:
        a = self._acl(acl_id)
        if not self._can_manage(UUID(a["data_id"]), operator):
            raise Forbidden("OWNER_REQUIRED", "只有 Owner 可以修改授权")
        old = dict(a)
        if permission:
            a["permission"] = permission
        if include_sub is not None:
            a["include_sub"] = include_sub
        try:
            self._guard_owner(UUID(a["data_id"]))
        except Conflict:
            a.update(old)
            raise

    async def delete_team_grant(self, acl_id: str, operator: UUID) -> None:
        a = self._acl(acl_id)
        if not self._can_manage(UUID(a["data_id"]), operator):
            raise Forbidden("OWNER_REQUIRED", "只有 Owner 可以修改授权")
        self.acl.remove(a)
        try:
            self._guard_owner(UUID(a["data_id"]))
        except Conflict:
            self.acl.append(a)
            raise

    async def rename_team(self, team_uuid: UUID, name: str) -> None:
        self.names[str(team_uuid)] = name

    async def aclose(self) -> None:
        return None


class FakeAtlas:
    """Agent 列表 + 会话 / 运行 / 事件流。

    create_run 立刻把这一轮的事件写进会话流（started → delta… → completed → finished），
    回复内容由 reply(指令) 决定；fail_next 置位时这一轮失败。
    """

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, list[AtlasEvent]] = {}
        self.inputs: dict[str, list[str]] = {}
        self.fail_next = False
        self.version = 0
        self._cond = asyncio.Condition()
        self._idem: dict[str, str] = {}

    def reply(self, text: str) -> str:
        self.version += 1
        body = f"# 产物 第 {self.version} 稿\n\n依据：{text[:30]}"
        return f"好的，已按要求产出。\n<artifact>\n{body}\n</artifact>"

    async def create_thread(self, agent_id: str, title: str) -> str:
        tid = str(uuid.uuid4())
        self.threads[tid], self.inputs[tid] = [], []
        return tid

    async def create_run(self, thread_id: str, text: str, idempotency_key: str) -> str:
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        rid = str(uuid.uuid4())
        self._idem[idempotency_key] = rid
        self.inputs[thread_id].append(text)
        evs = self.threads[thread_id]

        def add(type_: str, data: dict[str, Any]) -> None:
            evs.append(AtlasEvent(type=type_, run_id=rid, thread_seq=len(evs) + 1, data=data))

        add("run.started", {})
        if self.fail_next:
            self.fail_next = False
            add("run.failed", {"message": "模型网关超时"})
        else:
            out = self.reply(text)
            for i in range(0, len(out), 20):
                add("message.delta", {"text": out[i : i + 20], "block": 0})
            add("message.completed", {"content": [{"type": "text", "text": out}]})
            add("run.finished", {})
        async with self._cond:
            self._cond.notify_all()
        return rid

    async def stream_thread(self, thread_id: str, after_seq: int) -> AsyncIterator[AtlasEvent]:
        cursor = after_seq
        while True:
            evs = self.threads[thread_id]
            for ev in evs[cursor:]:
                cursor = ev.thread_seq
                yield ev
            async with self._cond:
                await self._cond.wait()

    def add(self, name: str, status: str = "enabled") -> str:
        aid = str(uuid.uuid4())
        self.agents[aid] = {
            "id": aid,
            "name": name,
            "description": f"{name} 的说明",
            "avatar_key": "robot",
            "model": "claude-sonnet-5",
            "status": status,
        }
        return aid

    async def list_agents(
        self, *, q: str | None = None, status: str | None = "enabled"
    ) -> list[dict[str, Any]]:
        return [
            a
            for a in self.agents.values()
            if (not status or a["status"] == status) and (not q or q in a["name"])
        ]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self.agents.get(agent_id)

    async def aclose(self) -> None:
        return None
