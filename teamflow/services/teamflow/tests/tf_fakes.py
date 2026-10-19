"""用户中心 / Atlas / 产物仓库的内存实现：
与 uc.UCClient、atlas.AtlasClient、RepoHost 的方法签名一致。

规则按用户中心的真实行为简化：
- 授权快照 = 角色 → 操作码（取自 catalog），菜单从略；
- 数据权限：用户直授或所在部门授权（include_sub 按部门树判断），取最高级别；
- 数据编码级授权（「全部团队」）：对每个团队的有效级别 = max(团队授权, 全部团队授权)；
  不算团队成员（member_only 不含它），不计入「至少保留一个 Owner」；
- 数据已有授权时写入须带 operator，且其有效级别为 Owner；至少保留一个 Owner。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from atlas_teamflow import catalog
from atlas_teamflow.atlas import AtlasEvent
from atlas_teamflow.errors import Conflict, Forbidden, Invalid, NotFound, Upstream
from atlas_teamflow.outbound import outbound_headers
from atlas_teamflow.process.repo_host import CommitInfo, RepoFile, RepoInfo

LEVELS = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}
NAMES = {v: k for k, v in LEVELS.items()}
ROLE_OPS = {r["code"]: set(r["operations"]) for r in catalog.ROLES}


class FakeUC:
    def __init__(self) -> None:
        self.users: dict[UUID, dict[str, Any]] = {}
        self.dept_map: dict[UUID, dict[str, Any]] = {}
        self.acl: list[dict[str, Any]] = []
        #: 数据编码级授权（对全部团队）：{permission, subject_type, subject_id, include_sub}
        self.type_acl: list[dict[str, Any]] = []
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

    def grant_all_teams(self, user_uuid: UUID, permission: str) -> None:
        """测试用：在用户中心给某人授「全部团队」级别（数据编码级授权）。"""
        self.type_acl.append(
            {
                "permission": permission,
                "subject_type": "USER",
                "subject_id": str(user_uuid),
                "include_sub": False,
            }
        )

    def type_level(self, user_uuid: UUID) -> int:
        u = self._user(user_uuid)
        return max((LEVELS[a["permission"]] for a in self.type_acl if self._hits(a, u)), default=0)

    def level_of(self, team_uuid: UUID, user_uuid: UUID, *, member_only: bool = False) -> int:
        u = self._user(user_uuid)
        own = max(
            (LEVELS[a["permission"]] for a in self._rows(team_uuid) if self._hits(a, u)), default=0
        )
        return own if member_only else max(own, self.type_level(user_uuid))

    def _can_manage(self, team_uuid: UUID, operator: UUID) -> bool:
        return self.level_of(team_uuid, operator) == 3

    def _guard_owner(self, team_uuid: UUID) -> None:
        if not any(a["permission"] == "OWNER" for a in self._rows(team_uuid)):
            raise Conflict("LAST_OWNER", "至少保留一个 Owner")

    async def team_level(self, team_uuid: UUID, user_uuid: UUID) -> str:
        return NAMES[self.level_of(team_uuid, user_uuid)]

    async def accessible_teams(
        self, user_uuid: UUID, min_level: str = "WRITE", *, member_only: bool = False
    ) -> dict[str, str]:
        out = {}
        for tid in {a["data_id"] for a in self.acl}:
            lv = self.level_of(UUID(tid), user_uuid, member_only=member_only)
            if lv >= LEVELS[min_level]:
                out[tid] = NAMES[lv]
        return out

    async def all_teams_level(self, user_uuid: UUID) -> str:
        return NAMES[self.type_level(user_uuid)]

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
            lv = self.level_of(team_uuid, uid, member_only=True)  # 持有人不含「全部团队」授权
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


@dataclass
class _Commit:
    info: CommitInfo
    changed: list[str]
    tree: dict[str, str] = field(default_factory=dict)


class MemoryRepoHost:
    """产物仓库的内存实现：提交由测试（模拟 Agent / 成员在本地）用 push 写入，TeamFlow 只读。"""

    namespace = "tf-test"

    def __init__(self) -> None:
        self.repos: dict[str, list[_Commit]] = {}  # 新的在前
        self.collab: dict[str, dict[str, str]] = {}
        self.fail_ensure = False

    async def prepare(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def repo_name(self, project_uuid: UUID) -> str:
        return f"teamflow-{project_uuid.hex}"

    def info(self, repo: str) -> RepoInfo:
        web = f"https://gitee.test/{self.namespace}/{repo}"
        return RepoInfo(f"{self.namespace}/{repo}", web, f"{web}.git")

    async def ensure_repo(self, repo: str, description: str) -> RepoInfo:
        if self.fail_ensure:
            raise Upstream("GITEE_UNAVAILABLE", "Gitee 暂不可用")
        self.repos.setdefault(repo, [])
        self.collab.setdefault(repo, {})
        return self.info(repo)

    def push(
        self,
        repo: str,
        files: dict[str, str],
        message: str,
        *,
        author_login: str | None = None,
        author_name: str = "TeamFlow Agent",
    ) -> str:
        history = self.repos[repo]
        tree = {**(history[0].tree if history else {}), **files}
        sha = hashlib.sha1(f"{repo}{len(history)}{message}".encode()).hexdigest()
        info = CommitInfo(
            sha=sha,
            message=message,
            committed_at=datetime.now(UTC),
            author_name=author_name,
            author_email=f"{author_login or 'agent'}@x.com",
            author_login=author_login,
            web_url=f"{self.info(repo).web_url}/commit/{sha}",
        )
        history.insert(0, _Commit(info, list(files), tree))
        return sha

    def last(self, repo: str) -> CommitInfo:
        return self.repos[repo][0].info

    async def commits(
        self, repo: str, *, path: str | None = None, limit: int = 20
    ) -> list[CommitInfo]:
        hits = [c.info for c in self.repos.get(repo, []) if path is None or path in c.changed]
        return hits[:limit]

    async def resolve(self, repo: str, ref: str) -> str | None:
        history = self.repos.get(repo, [])
        if ref == "master":
            return history[0].info.sha if history else None
        return next((c.info.sha for c in history if c.info.sha == ref), None)

    async def tree(self, repo: str, sha: str, prefix: str) -> tuple[list[RepoFile], bool]:
        c = next(c for c in self.repos[repo] if c.info.sha == sha)
        files = [RepoFile(p, len(v.encode())) for p, v in c.tree.items() if p.startswith(prefix)]
        return sorted(files, key=lambda f: f.path), False

    async def read_file(self, repo: str, path: str, ref: str) -> str | None:
        c = next((c for c in self.repos.get(repo, []) if c.info.sha == ref), None)
        return c.tree.get(path) if c else None

    async def collaborators(self, repo: str) -> dict[str, str]:
        return dict(self.collab.get(repo, {}))

    async def set_collaborator(self, repo: str, login: str, permission: str) -> None:
        self.collab.setdefault(repo, {})[login] = permission

    async def remove_collaborator(self, repo: str, login: str) -> None:
        self.collab.get(repo, {}).pop(login, None)


_CLONE = re.compile(r"git clone (\S+?)(?:\.git)? /repos/")
_OUTPUT = re.compile(r"写入 `([^`]+)`")
_MESSAGE = re.compile(r"提交说明：\n```\n(.*?)\n```", re.S)


class FakeAtlas:
    """Agent 列表 + 会话 / 运行 / 事件流。

    create_run 立刻把这一轮的事件写进会话流（started → delta… → completed → finished），
    回复内容由 reply(指令) 决定；fail_next 置位时这一轮失败。
    ★ 模拟能执行命令的 Agent：指令里带 git 操作说明时记下仓库 / 产物路径 / 提交说明，
      之后每轮都把产物提交到 host（commits=False 时模拟「不会执行命令」的 Agent）。
    """

    def __init__(self) -> None:
        self.host: MemoryRepoHost | None = None
        self.commits = True
        #: 每次建会话 / 发指令时 TeamFlow 带的身份请求头（outbound_headers）：(动作, 头)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.git_ctx: dict[str, tuple[str, str, str]] = {}
        self.thread_profiles: dict[str, str | None] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, list[AtlasEvent]] = {}
        self.inputs: dict[str, list[str]] = {}
        self.fail_next = False
        self.version = 0
        #: run → 会话；会话 → 消息（透传接口用）
        self.run_thread: dict[str, str] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self._cond = asyncio.Condition()
        self._idem: dict[str, str] = {}

    def reply(self, thread_id: str, text: str) -> str:
        self.version += 1
        body = f"# 产物 第 {self.version} 稿\n\n依据：{text[:30]}"
        ctx = self.git_ctx.get(thread_id)
        if not (self.commits and self.host and ctx):
            return f"好的，已按要求产出。\n\n{body}"
        repo, out, template = ctx
        sha = self.host.push(
            repo, {out: body}, template.replace("<一句话说明本次修改>", "按要求产出")
        )
        return f"已提交 {sha[:8]}：按要求产出。"

    async def create_thread(
        self, agent_id: str, title: str, *, git_profile: str | None = None
    ) -> str:
        self.calls.append(("create_thread", outbound_headers()))
        tid = str(uuid.uuid4())
        self.thread_profiles[tid] = git_profile
        self.threads[tid], self.inputs[tid] = [], []
        return tid

    async def create_run(self, thread_id: str, text: str, idempotency_key: str) -> str:
        self.calls.append(("create_run", outbound_headers()))
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        rid = str(uuid.uuid4())
        self._idem[idempotency_key] = rid
        self.inputs[thread_id].append(text)
        clone, path, msg = _CLONE.search(text), _OUTPUT.search(text), _MESSAGE.search(text)
        if clone and path and msg:  # 记下 git 操作说明（即使这一轮失败，下一轮仍按它提交）
            repo = clone.group(1).rsplit("/", 1)[-1]
            self.git_ctx[thread_id] = (repo, path.group(1), msg.group(1))
        self.run_thread[rid] = thread_id
        self.messages.setdefault(thread_id, []).append(
            {
                "id": str(uuid.uuid4()),
                "thread_id": thread_id,
                "run_id": rid,
                "role": "user",
                "content": [{"type": "text", "text": text}],
            }
        )
        evs = self.threads[thread_id]

        def add(type_: str, data: dict[str, Any]) -> None:
            evs.append(AtlasEvent(type=type_, run_id=rid, thread_seq=len(evs) + 1, data=data))

        add("run.started", {})
        if self.fail_next:
            self.fail_next = False
            add("run.failed", {"message": "模型网关超时"})
        else:
            out = self.reply(thread_id, text)
            for i in range(0, len(out), 20):
                add("message.delta", {"text": out[i : i + 20], "block": 0})
            add("message.completed", {"content": [{"type": "text", "text": out}]})
            self.messages[thread_id].append(
                {
                    "id": str(uuid.uuid4()),
                    "thread_id": thread_id,
                    "run_id": rid,
                    "role": "assistant",
                    "content": [{"type": "text", "text": out}],
                }
            )
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

    def add(self, name: str, status: str = "enabled", kind: str = "acp") -> str:
        aid = str(uuid.uuid4())
        self.agents[aid] = {
            "id": aid,
            "name": name,
            "description": f"{name} 的说明",
            "avatar_key": "robot",
            "model": "claude-sonnet-5",
            "status": status,
            "kind": kind,
            "subagents": [],
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

    # ── 透传接口（人机协同代理）：按 Atlas 的路径与响应形状模拟
    async def raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        parts = path.strip("/").split("/")  # v1 / threads|runs / id / ...
        if parts[1] == "threads" and parts[2] in self.threads:
            tid = parts[2]
            if len(parts) == 3 and method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": tid,
                        "title": "节点会话",
                        "agent_name": "全能 Agent",
                        "agent_avatar_key": "robot",
                        "parent_thread_id": None,
                    },
                )
            if parts[3:] == ["messages"] and method == "GET":
                return httpx.Response(
                    200,
                    json={"data": list(reversed(self.messages.get(tid, []))), "next_cursor": None},
                )
            if parts[3:] == ["runs"] and method == "POST":
                text = "".join(b.get("text", "") for b in (json_body or {}).get("content", []))
                key = (headers or {}).get("idempotency-key") or str(uuid.uuid4())
                rid = await self.create_run(tid, text, key)
                return httpx.Response(
                    202, json={"run_id": rid, "message_id": str(uuid.uuid4()), "status": "queued"}
                )
        if parts[1] == "runs" and parts[2] in self.run_thread:
            rid = parts[2]
            if len(parts) == 3:
                return httpx.Response(
                    200, json={"id": rid, "thread_id": self.run_thread[rid], "status": "succeeded"}
                )
            if parts[3:] == ["cancel"] and method == "POST":
                return httpx.Response(
                    200, json={"id": rid, "thread_id": self.run_thread[rid], "status": "cancelled"}
                )
        return httpx.Response(404, json={"error": {"kind": "not_found", "message": "not found"}})

    async def raw_stream(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, AsyncIterator[bytes]]:
        tid = path.strip("/").split("/")[2]
        evs = list(self.threads.get(tid, []))

        async def body() -> AsyncIterator[bytes]:
            for ev in evs:  # 测试用：回放已有事件后结束（真实的会话流不会自己结束）
                frame = {
                    "type": ev.type,
                    "run_id": ev.run_id,
                    "thread_seq": ev.thread_seq,
                    "data": ev.data,
                }
                data = json.dumps(frame, ensure_ascii=False)
                yield f"id: {ev.thread_seq}\nevent: {ev.type}\ndata: {data}\n\n".encode()

        return 200, body()


class FakeOAuth:
    """Gitee OAuth 的内存实现：code → Gitee 用户。"""

    enabled = True

    def __init__(self) -> None:
        self.users: dict[str, Any] = {}

    def authorize_url(self, state: str) -> str:
        return f"https://gitee.test/oauth/authorize?state={state}"

    async def exchange(self, code: str) -> Any:
        from atlas_teamflow.gitee_oauth import GiteeUser

        if code not in self.users:
            raise Invalid("GITEE_OAUTH_FAILED", "Gitee 授权失败或已过期，请重新绑定")
        login, uid = self.users[code]
        return GiteeUser(id=uid, login=login, name=login)

    async def aclose(self) -> None:
        return None
