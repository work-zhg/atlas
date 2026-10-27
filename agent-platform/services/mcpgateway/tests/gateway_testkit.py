"""MCP 网关测试的构造工具（不叫 helpers：避免与别处同名模块撞车）。"""

from __future__ import annotations

import itertools
from typing import Any

from atlas_mcpgateway.errors import Conflict, Forbidden, Unavailable
from atlas_mcpgateway.perms import LEVEL_RANK, Grant, SubjectServices

#: 测试里的用户（用户中心的 uid）
ADMIN = "00000000-0000-7000-8000-00000000000a"
ALICE = "00000000-0000-7000-8000-0000000000a1"
BOB = "00000000-0000-7000-8000-0000000000b0"
DIGITAL = "00000000-0000-7000-8000-0000000000de"


def as_user(uid: str) -> dict[str, str]:
    return {"X-Uid": uid}


class FakePermissions:
    """用户中心数据权限的内存替身：服务名 → {用户: 级别}；只有直接授给用户的。"""

    def __init__(self) -> None:
        self.grants: dict[str, dict[str, str]] = {}
        #: 「全部 MCP 服务」这类编码级授权：用户 → 级别
        self.type_levels: dict[str, str] = {}
        self.users: dict[str, dict[str, Any]] = {
            ADMIN: {"id": ADMIN, "name": "管理员", "user_type": "human", "status": "active"},
            ALICE: {"id": ALICE, "name": "alice", "user_type": "human", "status": "active"},
            BOB: {"id": BOB, "name": "bob", "user_type": "human", "status": "active"},
            DIGITAL: {
                "id": DIGITAL,
                "name": "工单助手",
                "user_type": "digital",
                "status": "active",
            },
        }
        self.down = False
        self.calls = 0
        self._acl_ids = itertools.count(1)
        self.acl_rows: dict[str, tuple[str, str]] = {}

    def _check(self) -> None:
        self.calls += 1
        if self.down:
            raise Unavailable("用户中心暂不可用", code="AUTHZ_UNAVAILABLE")

    def _level(self, user_id: str, service: str) -> str:
        own = self.grants.get(service, {}).get(user_id, "NONE")
        typ = self.type_levels.get(user_id, "NONE")
        return own if LEVEL_RANK[own] >= LEVEL_RANK[typ] else typ

    def _accessible(self, user_id: str, min_level: str) -> SubjectServices:
        names = {
            s for s in self.grants if LEVEL_RANK[self._level(user_id, s)] >= LEVEL_RANK[min_level]
        }
        typ = self.type_levels.get(user_id, "NONE")
        return SubjectServices(frozenset(names), LEVEL_RANK[typ] >= LEVEL_RANK[min_level])

    async def usable(self, subject: str) -> SubjectServices:
        self._check()
        return self._accessible(subject, "READ")

    async def owned(self, user_id: str) -> SubjectServices:
        self._check()
        return self._accessible(user_id, "OWNER")

    async def level(self, user_id: str, service: str) -> str:
        self._check()
        return self._level(user_id, service)

    async def register(self, service: str, display_name: str, owner: str) -> None:
        self._check()
        if self.grants.get(service):
            raise Conflict(f"用户中心里已经登记过 MCP 服务 {service}", code="UC_REGISTERED")
        self.grants[service] = {owner: "OWNER"}

    async def rename(self, service: str, display_name: str) -> None:
        self._check()

    async def acl(self, service: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {"id": acl_id, "subject_id": uid, "permission": self.grants[svc][uid]}
            for acl_id, (svc, uid) in self.acl_rows.items()
            if svc == service and uid in self.grants.get(svc, {})
        ]

    async def grant(self, service: str, operator: str, grants: list[Grant]) -> None:
        self._check()
        if self._level(operator, service) != "OWNER":
            raise Forbidden("只有所有者可以授权", code="NOT_DATA_OWNER")
        for g in grants:
            self.grants.setdefault(service, {})[g.subject_id] = g.level
            self.acl_rows[str(next(self._acl_ids))] = (service, g.subject_id)

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None:
        self._check()
        service, uid = self.acl_rows[acl_id]
        if level is not None:
            self.grants[service][uid] = level

    async def revoke(self, acl_id: str, operator: str) -> None:
        self._check()
        service, uid = self.acl_rows.pop(acl_id)
        if self._level(operator, service) != "OWNER":
            raise Forbidden("只有所有者可以收回授权", code="NOT_DATA_OWNER")
        self.grants[service].pop(uid, None)

    async def holders(self, service: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "user": {"id": uid, "name": self.users.get(uid, {}).get("name", uid)},
                "permission": lv,
            }
            for uid, lv in self.grants.get(service, {}).items()
        ]

    async def user(self, user_id: str) -> dict[str, Any] | None:
        self._check()
        return self.users.get(user_id)

    def allow(self, service: str, *users: str) -> None:
        for uid in users:
            self.grants.setdefault(service, {})[uid] = "READ"


# ───────────────────────────── 场景


def weather_and_ticket(higress: Any) -> None:
    """两个要求网关凭据的服务 + 一个漏开了认证的服务（同 demo/mcp-gateway）。"""
    from fake_higress import FakeService

    weather = FakeService()
    weather.tool(
        "get_weather",
        "查询某个城市今天的天气。",
        lambda args, _h: f"{args.get('city')}：小雨",
        properties={"city": {"type": "string"}},
        read_only=True,
    )
    ticket = FakeService()
    ticket.tool(
        "create_ticket",
        "在工单系统里建一张工单，返回工单号。",
        lambda args, h: (
            f"T-001 {args.get('title')} 提交人={h.get('x-auth-uid')} 使用者={h.get('x-uid')}"
        ),
        properties={"title": {"type": "string"}},
    )
    ticket.tool("delete_ticket", "删除一张工单。", lambda args, _h: "deleted")
    report = FakeService(open=True)
    report.tool("daily_report", "今天的运营日报。", lambda _a, _h: "一切正常")
    higress.services.update({"weather": weather, "ticket": ticket, "report": report})


async def register(
    client: Any, name: str, *, review: bool = True, upstream: str | None = None
) -> Any:
    r = await client.post(
        "/api/v1/services",
        json={"name": name, "review_required": review, "upstream_name": upstream or name},
        headers=as_user(ADMIN),
    )
    return r


async def approve(client: Any, service: str, tool: str) -> Any:
    detail = (await client.get(f"/api/v1/services/{service}", headers=as_user(ADMIN))).json()
    digest = next(t["digest"] for t in detail["tools"] if t["name"] == tool)
    r = await client.post(
        f"/api/v1/services/{service}/tools/{tool}/review",
        json={"digest": digest, "decision": "approved"},
        headers=as_user(ADMIN),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def issue_key(client: Any, subject: str) -> str:
    """发 Key，经内部接口取回原文（管理接口不返回原文）。"""
    r = await client.post("/api/v1/keys", json={"subject_uid": subject}, headers=as_user(ADMIN))
    assert r.status_code == 201, r.text
    access = (await client.get(f"/internal/subjects/{subject}/access")).json()
    return str(access["key"])


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
