"""MCP 网关测试的构造工具（不叫 helpers：避免与别处同名模块撞车）。"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from atlas_mcpgateway.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from atlas_mcpgateway.perms import DATA_SERVER, DATA_TOOL, AclEntry, KeyAccess

#: 测试里的人（用户中心的 uid）
ADMIN = "00000000-0000-7000-8000-00000000000a"
ALICE = "00000000-0000-7000-8000-0000000000a1"
BOB = "00000000-0000-7000-8000-0000000000b0"
CAROL = "00000000-0000-7000-8000-0000000000c0"


def as_user(uid: str) -> dict[str, str]:
    return {"X-Uid": uid}


ATLAS = {"X-Caller-App": "atlas"}


@dataclass
class _Row:
    id: str
    level: str
    subject: str


class FakePermissions:
    """用户中心的内存替身。规则照用户中心：

    - 数据对象第一次写授权不需要操作人，但必须至少有一条 OWNER；已经有授权再「第一次写」= 冲突
    - 之后改授权，操作人要是这条数据的 Owner（或有这个编码的编码级 Owner）
    - 主体停用后对它一律「无权限」
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], list[_Row]] = {}
        #: 编码级 OWNER（管理员）
        self.type_owners: dict[str, set[str]] = {DATA_SERVER: {ADMIN}, DATA_TOOL: {ADMIN}}
        self.users: dict[str, dict[str, Any]] = {
            ADMIN: {
                "id": ADMIN,
                "name": "管理员",
                "account": "admin",
                "user_type": "human",
                "status": "active",
            },
            ALICE: {
                "id": ALICE,
                "name": "alice",
                "account": "alice",
                "user_type": "human",
                "status": "active",
            },
            BOB: {
                "id": BOB,
                "name": "bob",
                "account": "bob",
                "user_type": "human",
                "status": "active",
            },
            CAROL: {
                "id": CAROL,
                "name": "carol",
                "account": "carol",
                "user_type": "human",
                "status": "active",
            },
        }
        #: 外部引用 → 主体 uid（Key 的主体，数字员工）
        self.subjects: dict[str, str] = {}
        self.down = False
        self.calls = 0
        self._ids = itertools.count(1)

    def _check(self) -> None:
        self.calls += 1
        if self.down:
            raise Unavailable("用户中心暂不可用", code="AUTHZ_UNAVAILABLE")

    def _active(self, uid: str) -> bool:
        return self.users.get(uid, {}).get("status") == "active"

    def _owner(self, code: str, data_id: str, user: str) -> bool:
        if user in self.type_owners.get(code, set()):
            return True
        return any(
            r.subject == user and r.level == "OWNER" for r in self.objects.get((code, data_id), [])
        )

    def _ids_with(self, code: str, subject: str, level: str) -> frozenset[str]:
        rank = {"READ": 1, "OWNER": 3}
        return frozenset(
            data_id
            for (c, data_id), rows in self.objects.items()
            if c == code
            and any(r.subject == subject and rank[r.level] >= rank[level] for r in rows)
        )

    # ── Permissions

    async def key_access(self, subject: str) -> KeyAccess:
        self._check()
        if not self._active(subject):
            return KeyAccess()
        return KeyAccess(
            self._ids_with(DATA_SERVER, subject, "READ"), self._ids_with(DATA_TOOL, subject, "READ")
        )

    async def owned(self, user: str) -> tuple[frozenset[str], bool]:
        self._check()
        return self._ids_with(DATA_SERVER, user, "OWNER"), user in self.type_owners[DATA_SERVER]

    async def register(
        self,
        data_code: str,
        data_id: str,
        name: str,
        *,
        owners: list[str],
        readers: list[str] | None = None,
    ) -> None:
        self._check()
        if self.objects.get((data_code, data_id)):
            raise Conflict(f"已经登记过 {data_code} {data_id}", code="UC_REGISTERED")
        if not owners:
            raise Invalid("第一次授权中必须至少有一条 OWNER", code="OWNER_REQUIRED")
        rows = [_Row(str(next(self._ids)), "OWNER", u) for u in owners]
        rows += [_Row(str(next(self._ids)), "READ", u) for u in readers or []]
        self.objects[(data_code, data_id)] = rows

    async def rename(self, data_code: str, data_id: str, name: str) -> None:
        self._check()

    async def drop(self, data_code: str, data_id: str) -> None:
        self._check()
        self.objects.pop((data_code, data_id), None)

    async def acl(self, data_code: str, data_id: str) -> list[AclEntry]:
        self._check()
        return [
            AclEntry(
                r.id, r.level, "USER", r.subject, self.users.get(r.subject, {}).get("name", "")
            )
            for r in self.objects.get((data_code, data_id), [])
        ]

    async def grant(
        self, data_code: str, data_id: str, operator: str, *, level: str, users: list[str]
    ) -> None:
        self._check()
        rows = self.objects.get((data_code, data_id))
        if rows is None:
            raise NotFound("数据不存在", code="DATA_NOT_FOUND")
        if not self._owner(data_code, data_id, operator):
            raise Forbidden("只有 Owner 可以修改授权", code="NOT_DATA_OWNER")
        for u in users:
            existing = next((r for r in rows if r.subject == u), None)
            if existing is not None:
                existing.level = level
            else:
                rows.append(_Row(str(next(self._ids)), level, u))

    async def revoke(self, acl_id: str, operator: str) -> None:
        self._check()
        for (code, data_id), rows in self.objects.items():
            row = next((r for r in rows if r.id == acl_id), None)
            if row is None:
                continue
            if not self._owner(code, data_id, operator):
                raise Forbidden("只有 Owner 可以修改授权", code="NOT_DATA_OWNER")
            if row.level == "OWNER" and sum(1 for r in rows if r.level == "OWNER") == 1:
                raise Conflict("不能删掉最后一个 Owner", code="LAST_DATA_OWNER")
            rows.remove(row)
            return
        raise NotFound("授权不存在")

    async def user(self, uid: str) -> dict[str, Any] | None:
        self._check()
        return self.users.get(uid)

    async def search_users(self, q: str) -> list[dict[str, Any]]:
        self._check()
        return [u for u in self.users.values() if u["user_type"] == "human" and q in u["name"]]

    async def create_subject(self, *, ref: str, name: str, owner: str) -> str:
        self._check()
        if ref in self.subjects:
            return self.subjects[ref]
        if not self._active(owner) or self.users[owner]["user_type"] != "human":
            raise Invalid("负责人必须是未停用的人", code="OWNER_NOT_HUMAN")
        uid = f"00000000-0000-7000-9000-{len(self.subjects) + 1:012d}"
        self.subjects[ref] = uid
        self.users[uid] = {
            "id": uid,
            "name": name[:32],
            "account": f"dw-{uid[-6:]}",
            "user_type": "digital",
            "status": "active",
        }
        return uid

    async def set_subject_active(self, uid: str, active: bool) -> None:
        self._check()
        if uid in self.users:
            self.users[uid]["status"] = "active" if active else "disabled"

    # ── 断言用

    def readers(self, code: str, data_id: str) -> set[str]:
        return {r.subject for r in self.objects.get((code, data_id), []) if r.level == "READ"}

    def owners_of(self, code: str, data_id: str) -> set[str]:
        return {r.subject for r in self.objects.get((code, data_id), []) if r.level == "OWNER"}


# ───────────────────────────── 场景


async def apply_user_key(client: Any, uid: str) -> dict[str, Any]:
    r = await client.post("/api/v1/keys/me", headers=as_user(uid))
    assert r.status_code == 201, r.text
    return dict(r.json())


async def agent_key(
    client: Any, code: str, *, owner: str = ALICE, name: str = ""
) -> dict[str, Any]:
    r = await client.post(
        "/internal/client-keys",
        json={
            "client_type": "Agent",
            "client_code": code,
            "name": name or code,
            "owner_uid": owner,
        },
        headers=ATLAS,
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def key_id_of(client: Any, subject_uid: str) -> str:
    keys = (await client.get("/api/v1/keys", headers=as_user(ADMIN))).json()
    return str(next(k["id"] for k in keys if k["subject_uid"] == subject_uid))


async def grant(
    client: Any,
    service: str,
    key_id: str,
    scope: str,
    tools: list[str] | None = None,
    *,
    by: str = ADMIN,
) -> Any:
    return await client.put(
        f"/api/v1/services/{service}/grants/{key_id}",
        json={"scope": scope, "tools": tools or []},
        headers=as_user(by),
    )


def mcp_client(app: Any, service: str, key: str | None, **headers: str) -> Any:
    """FastMCP 客户端，经 ASGITransport 连进程内的网关（和 Atlas / Pod 里的 CLI 同一种客户端）。"""
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def factory(headers: Any = None, timeout: Any = None, auth: Any = None, **_: Any) -> Any:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth
        )

    sent = dict(headers)
    if key is not None:
        sent["Authorization"] = f"Bearer {key}"
    transport = StreamableHttpTransport(
        f"http://gateway/mcp/{service}", headers=sent, httpx_client_factory=factory
    )
    return Client(transport, mode="legacy")
