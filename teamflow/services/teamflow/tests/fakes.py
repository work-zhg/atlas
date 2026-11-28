"""用户中心、Agent 平台的内存实现：和 usercenter.UserCenter、agentplatform.AgentPlatform
的方法签名一致。

规则按真实行为简化：
- 用户中心：授权快照 = 用户直接带的操作码；数据权限只有授给人的；有效级别 = max(单条, 编码级)；
  数据已有授权时写入要带 operator 且他是 Owner；至少保留一个 Owner。
- Agent 平台：智能体的 Owner 是建它的人；授权要当前出站身份（X-Uid）是 Owner；
  会话串行（有没结束的 run 时开新 run → RunBusy）；开 run 按幂等键去重；应用工作空间按编号幂等。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from atlas_teamflow.agentplatform import RunBusy
from atlas_teamflow.errors import Forbidden, Invalid, NotFound, TFError
from atlas_teamflow.outbound import outbound_headers
from atlas_tenancy import TenantScope

LEVELS = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}
NAMES = {v: k for k, v in LEVELS.items()}

#: 测试用的系统角色 → 操作码（代码不登记角色；运营人员按 README 在运营中心录入）
TEAM_ADMIN_OPS = {"team:create", "team:config", "flow:config", "process:run", "process:view"}
TEAM_MEMBER_OPS = {"process:run", "process:view"}


def _me() -> str:
    return outbound_headers().get("X-Uid", "")


def _need_tenant() -> None:
    """真的用户中心、Agent 平台按请求头 X-Tenant-Id 进入租户：出站调用时必须已经在某个租户里。"""
    assert TenantScope.current() is not None, "出站调用没有租户上下文（X-Tenant-Id）"


class FakeUC:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.ops: dict[str, set[str]] = {}
        self.tenant_list: list[dict[str, Any]] = []
        #: (编码, 数据 id) → [{id, subject: {type, id, name}, permission}]
        self.acls: dict[tuple[str, str], list[dict[str, Any]]] = {}
        #: (编码, 用户) → 级别
        self.type_grants: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, Any]] = []

    # ── 造数据
    def add_tenant(self, name: str, *, system: bool = False) -> UUID:
        tid = uuid.uuid4()
        self.tenant_list.append(
            {"id": str(tid), "code": name, "name": name, "status": "active", "is_system": system}
        )
        return tid

    def add_user(self, account: str, name: str, tenant: UUID, ops: set[str] | None = None) -> UUID:
        uid = uuid.uuid4()
        self.users[str(uid)] = {
            "id": str(uid),
            "account": account,
            "name": name,
            "status": "active",
            "tenant_id": str(tenant),
        }
        self.ops[str(uid)] = set(ops or ())
        return uid

    def grant_type(self, code: str, uid: UUID, level: str) -> None:
        self.type_grants[(code, str(uid))] = level

    # ── 人、租户、快照
    async def authz(self, uid: UUID) -> dict[str, Any]:
        _need_tenant()
        u = self.users.get(str(uid))
        if u is None:
            raise NotFound("USER_NOT_FOUND", "用户不存在")
        ops = sorted(self.ops.get(str(uid), set()))
        return {
            "user": dict(u),
            "can_access": bool(ops),
            "roles": [],
            "permissions": ops,
            "menus": [],
        }

    async def user(self, uid: UUID) -> dict[str, Any]:
        _need_tenant()
        u = self.users.get(str(uid))
        if u is None:
            raise NotFound("USER_NOT_FOUND", "用户不存在")
        return dict(u)

    async def search_users(self, q: str | None, *, size: int = 20) -> list[dict[str, Any]]:
        _need_tenant()
        return [
            dict(u) for u in self.users.values() if not q or q in u["name"] or q in u["account"]
        ][:size]

    async def tenants(self) -> list[dict[str, Any]]:
        return [dict(t) for t in self.tenant_list]

    # ── 数据权限
    def _level(self, code: str, data_id: str, uid: str) -> str:
        best = LEVELS[self.type_grants.get((code, uid), "NONE")]
        for g in self.acls.get((code, data_id), []):
            if g["subject"]["id"] == uid:
                best = max(best, LEVELS[g["permission"]])
        return NAMES[best]

    async def data_level(self, code: str, data_id: UUID, uid: UUID) -> str:
        _need_tenant()
        return self._level(code, str(data_id), str(uid))

    async def type_level(self, code: str, uid: UUID) -> str:
        _need_tenant()
        return self.type_grants.get((code, str(uid)), "NONE")

    async def accessible(self, code: str, uid: UUID, min_level: str = "READ") -> dict[str, str]:
        _need_tenant()
        out = {}
        for (c, data_id), rows in self.acls.items():
            if c != code:
                continue
            for g in rows:
                if g["subject"]["id"] == str(uid) and LEVELS[g["permission"]] >= LEVELS[min_level]:
                    out[data_id] = g["permission"]
        return out

    async def write_grants(
        self,
        code: str,
        data_id: UUID,
        data_name: str,
        grants: list[dict[str, Any]],
        operator: UUID | None,
    ) -> dict[str, Any]:
        _need_tenant()
        key = (code, str(data_id))
        rows = self.acls.setdefault(key, [])
        if rows:
            if operator is None or self._level(code, str(data_id), str(operator)) != "OWNER":
                raise Forbidden("OWNER_REQUIRED", "数据已有授权，修改须带 operator 且其为 Owner")
        elif not any(g["permission"] == "OWNER" for g in grants):
            raise Invalid("OWNER_REQUIRED", "第一次授权中必须至少有一条 OWNER")
        for g in grants:
            sid = str(g["subject_id"])
            name = self.users.get(sid, {}).get("name", sid)
            for r in rows:
                if r["subject"]["id"] == sid:
                    r["permission"] = g["permission"]
                    break
            else:
                rows.append(
                    {
                        "id": uuid.uuid4().hex,
                        "subject": {"type": g.get("subject_type", "USER"), "id": sid, "name": name},
                        "permission": g["permission"],
                        "include_sub": False,
                    }
                )
        self.calls.append(("write_grants", (code, str(data_id), [g["subject_id"] for g in grants])))
        return {"added": len(grants)}

    async def acl(self, code: str, data_id: UUID) -> list[dict[str, Any]]:
        _need_tenant()
        return [dict(r) for r in self.acls.get((code, str(data_id)), [])]

    async def holders(
        self, code: str, data_id: UUID, min_level: str = "READ"
    ) -> list[dict[str, Any]]:
        _need_tenant()
        out = []
        for r in self.acls.get((code, str(data_id)), []):
            if LEVELS[r["permission"]] >= LEVELS[min_level] and r["subject"]["id"] in self.users:
                u = self.users[r["subject"]["id"]]
                out.append(
                    {
                        "user": {"id": u["id"], "name": u["name"], "account": u["account"]},
                        "permission": r["permission"],
                    }
                )
        return out

    def _row(self, acl_id: str) -> tuple[tuple[str, str], dict[str, Any]]:
        for key, rows in self.acls.items():
            for r in rows:
                if r["id"] == acl_id:
                    return key, r
        raise NotFound("GRANT_NOT_FOUND", "授权不存在")

    async def update_grant(self, acl_id: str, operator: UUID, *, permission: str) -> None:
        _need_tenant()
        key, r = self._row(acl_id)
        if self._level(key[0], key[1], str(operator)) != "OWNER":
            raise Forbidden("OWNER_REQUIRED", "要 Owner")
        r["permission"] = permission
        self._guard_owner(key)

    async def delete_grant(self, acl_id: str, operator: UUID) -> None:
        _need_tenant()
        key, r = self._row(acl_id)
        if self._level(key[0], key[1], str(operator)) != "OWNER":
            raise Forbidden("OWNER_REQUIRED", "要 Owner")
        self.acls[key].remove(r)
        self._guard_owner(key)

    def _guard_owner(self, key: tuple[str, str]) -> None:
        if not any(r["permission"] == "OWNER" for r in self.acls[key]):
            raise TFError("LAST_OWNER", "至少保留一个 Owner")

    async def delete_data(self, code: str, data_id: UUID) -> None:
        _need_tenant()
        self.acls.pop((code, str(data_id)), None)

    async def rename_data(self, code: str, data_id: UUID, name: str) -> None:
        _need_tenant()
        self.calls.append(("rename", (code, str(data_id), name)))


class FakeAtlas:
    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.idem: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, Any]] = []
        #: 下一次开 run 直接失败（模拟 Agent 平台不可用）
        self.fail_next_run: TFError | None = None
        #: 取消要一会儿才收尾：取消后 run 还算没结束，要 finish 才结束
        self.slow_cancel = False

    # ── 造数据
    def add_agent(
        self,
        name: str,
        owner: UUID,
        *,
        auth_subject: str = "agent",
        skills: list[str] | None = None,
    ) -> str:
        aid = str(uuid.uuid4())
        self.agents[aid] = {
            "id": aid,
            "name": name,
            "kind": "native",
            "model": "minimax",
            "status": "enabled",
            "auth_subject": auth_subject,
            "description": f"{name}的说明",
            "spec": {"skills": [{"slug": s, "name": s} for s in skills or []]},
            "owner": str(owner),
            "grants": [
                {
                    "id": uuid.uuid4().hex,
                    "subject": {"type": "USER", "id": str(owner)},
                    "level": "OWNER",
                }
            ],
        }
        return aid

    def users_with_use(self, agent_id: str) -> set[str]:
        return {g["subject"]["id"] for g in self.agents[agent_id]["grants"]}

    def _level(self, agent_id: str, uid: str) -> str:
        best = 0
        for g in self.agents[agent_id]["grants"]:
            if g["subject"]["id"] == uid:
                best = max(best, LEVELS[g["level"]])
        return NAMES[best]

    # ── 智能体
    async def list_agents(self, q: str | None = None) -> list[dict[str, Any]]:
        _need_tenant()
        me = _me()
        return [
            {k: v for k, v in a.items() if k not in ("grants", "owner")}
            for a in self.agents.values()
            if self._level(a["id"], me) != "NONE" and (not q or q in a["name"])
        ]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        _need_tenant()
        a = self.agents.get(agent_id)
        return {k: v for k, v in a.items() if k not in ("grants", "owner")} if a else None

    async def agent_grants(self, agent_id: str) -> dict[str, Any]:
        _need_tenant()
        a = self.agents[agent_id]
        return {"data": [dict(g) for g in a["grants"]], "my_level": self._level(agent_id, _me())}

    async def grant_use(self, agent_id: str, uids: list[UUID]) -> None:
        _need_tenant()
        if self._level(agent_id, _me()) != "OWNER":
            raise Forbidden("ATLAS_FORBIDDEN", "只有 Owner 能共享")
        for u in uids:
            self.agents[agent_id]["grants"].append(
                {"id": uuid.uuid4().hex, "subject": {"type": "USER", "id": str(u)}, "level": "READ"}
            )
        self.calls.append(("grant_use", (agent_id, [str(u) for u in uids])))

    # ── 应用工作空间
    async def open_workspace(self, space_uuid: UUID, title: str) -> dict[str, Any]:
        _need_tenant()
        self.workspaces.setdefault(
            str(space_uuid), {"title": title, "files": {}, "released": False}
        )
        self.calls.append(("open_workspace", str(space_uuid)))
        return {"space_uuid": str(space_uuid)}

    async def release_workspace(self, space_uuid: UUID) -> None:
        _need_tenant()
        if str(space_uuid) in self.workspaces:
            self.workspaces[str(space_uuid)]["released"] = True
        self.calls.append(("release_workspace", str(space_uuid)))

    async def list_files(self, space_uuid: UUID, dir: str) -> dict[str, Any]:
        _need_tenant()
        files = self.workspaces[str(space_uuid)]["files"]
        prefix = f"{dir}/" if dir else ""
        dirs, out = set(), []
        for path, data in files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                out.append({"name": rest, "path": path, "size": len(data), "modified_at": None})
        return {
            "dir": dir,
            "dirs": [{"name": d, "path": prefix + d} for d in sorted(dirs)],
            "files": out,
            "truncated": False,
        }

    async def stat_files(self, space_uuid: UUID, paths: list[str]) -> list[str]:
        _need_tenant()
        files = self.workspaces[str(space_uuid)]["files"]
        return [p for p in paths if p not in files]

    async def put_file(self, space_uuid: UUID, path: str, data: bytes) -> dict[str, Any]:
        _need_tenant()
        self.workspaces[str(space_uuid)]["files"][path] = data
        return {"path": path, "size": len(data)}

    async def download(self, space_uuid: UUID, path: str) -> tuple[int, AsyncIterator[bytes]]:
        _need_tenant()
        data = self.workspaces[str(space_uuid)]["files"].get(path)
        if data is None:
            raise NotFound("ATLAS_NOT_FOUND", "文件不存在")

        async def body() -> AsyncIterator[bytes]:
            yield data

        return len(data), body()

    # ── 会话
    async def create_thread(self, agent_id: str, *, title: str, app_workspace: UUID) -> str:
        _need_tenant()
        tid = str(uuid.uuid4())
        self.threads[tid] = {
            "agent": agent_id,
            "title": title,
            "space": str(app_workspace),
            "owner": _me(),
            "runs": [],
        }
        self.calls.append(("create_thread", tid))
        return tid

    async def create_run(
        self, thread_id: str, content: list[dict[str, Any]], *, idempotency_key: str
    ) -> str:
        _need_tenant()
        key = (_me(), idempotency_key)
        if key in self.idem:
            return self.idem[key]
        if self.fail_next_run is not None:
            err, self.fail_next_run = self.fail_next_run, None
            raise err
        t = self.threads[thread_id]
        if any(self.runs[r]["active"] for r in t["runs"]):
            raise RunBusy()
        rid = str(uuid.uuid4())
        self.runs[rid] = {
            "thread": thread_id,
            "content": content,
            "by": _me(),
            "active": True,
            "cancelled": False,
        }
        t["runs"].append(rid)
        self.idem[key] = rid
        self.calls.append(("create_run", rid))
        return rid

    async def cancel_run(self, run_id: str) -> None:
        _need_tenant()
        r = self.runs.get(run_id)
        if r is not None and r["active"]:
            r["cancelled"] = True
            r["active"] = self.slow_cancel
        self.calls.append(("cancel_run", run_id))

    def finish(self, run_id: str) -> None:
        self.runs[run_id]["active"] = False

    def texts(self, thread_id: str) -> list[str]:
        return [
            "\n".join(b.get("text", "") for b in self.runs[r]["content"])
            for r in self.threads[thread_id]["runs"]
        ]
