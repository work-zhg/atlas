"""register-uc 把 MCP 交给 MCP 网关（doc/mcp-design.html 第二版 §5.3）：Atlas 名下的操作码
mcp:manage、数据编码 AtlasMcpServer 删除之前，授权搬到网关的应用名下。用户中心是管理接口的替身。"""

from __future__ import annotations

import json
from typing import Any

import httpx
from atlas_agent.auth import register as reg

ADMIN = "u-admin"


class FakeUCAdmin:
    """用户中心管理接口的替身：只实现退役用到的几个，记下所有写操作。"""

    def __init__(self, *, gateway: bool = True, targets: tuple[str, ...] = ("serpapi",)) -> None:
        self.gateway = gateway
        self.targets = targets
        self.writes: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method != "GET":
            body = json.loads(request.content) if request.content else None
            self.writes.append((method, path, body))
            return httpx.Response(200, json={"ok": True})
        params = request.url.params
        routes: dict[str, Any] = {
            "/api/v1/apps": [{"id": "atlas", "code": "atlas"}]
            + ([{"id": "gw", "code": "mcp-gateway"}] if self.gateway else []),
            "/api/v1/apps/atlas/data-types": [{"id": "t-old", "code": "AtlasMcpServer"}],
            "/api/v1/apps/gw/data-types": [{"id": "t-new", "code": "McpServer"}],
            "/api/v1/apps/gw/roles": [{"id": "r-mcp", "code": "MCP_ADMIN"}],
            "/api/v1/apps/atlas/roles": [
                {"id": "r-admin", "code": "ATLAS_ADMIN"},
                {"id": "r-member", "code": "ATLAS_MEMBER"},
            ],
            "/api/v1/roles/r-admin": {"operation_ids": ["op-mcp", "op-chat"]},
            "/api/v1/roles/r-member": {"operation_ids": ["op-chat"]},
            "/api/v1/roles/r-admin/grants": {
                "grants": [{"subject": {"type": "user", "id": ADMIN}, "include_sub": False}],
                "positions": [
                    {"grants": [{"subject": {"type": "dept", "id": "d-ops"}, "include_sub": True}]}
                ],
            },
            "/api/v1/data-types/t-old/acl": {
                "acl": [
                    {
                        "permission": "READ",
                        "subject": {"type": "DEPT", "id": "d-root"},
                        "include_sub": True,
                    }
                ]
            },
            "/api/v1/data/o-old": {
                "acl": [
                    {
                        "level": "OWNER",
                        "subject": {"type": "user", "id": ADMIN},
                        "include_sub": False,
                    }
                ]
            },
            "/api/v1/auth/me": {"user": {"id": ADMIN}, "tenant": {"id": "t1"}},
        }
        if path == "/api/v1/data":
            if params["data_code"] == "AtlasMcpServer":
                items = [{"id": "o-old", "data_id": "serpapi"}]
            else:
                items = [{"id": f"o-new-{t}", "data_id": t} for t in self.targets]
            return httpx.Response(200, json={"total": len(items), "items": items})
        if path not in routes:
            return httpx.Response(404, json={"message": f"unexpected {path}"})
        return httpx.Response(200, json=routes[path])


def _uc(fake: FakeUCAdmin) -> reg._UC:
    uc = reg._UC("http://uc")
    uc.http = httpx.Client(base_url="http://uc", transport=httpx.MockTransport(fake.handler))
    return uc


OPS = {"mcp:manage": {"id": "op-mcp"}, "chat:use": {"id": "op-chat"}}


def test_nothing_is_deleted_before_the_gateway_is_registered() -> None:
    fake = FakeUCAdmin(gateway=False)
    keep = reg._retire_mcp(_uc(fake), "atlas", OPS)
    assert keep == {"mcp:manage"}
    assert fake.writes == []


def test_holders_and_grants_move_to_the_gateway_then_old_codes_go() -> None:
    fake = FakeUCAdmin()
    keep = reg._retire_mcp(_uc(fake), "atlas", OPS)
    assert keep == set()
    writes = [(m, p) for m, p, _ in fake.writes]
    # ① mcp:manage 的持有人（直接 + 经岗位）→ MCP_ADMIN +「全部 MCP 服务 · 所有者」
    roles = [b for m, p, b in fake.writes if p == "/api/v1/grants"]
    assert [r["subjects"][0]["id"] for r in roles] == [ADMIN, "d-ops"]
    assert all(r["target_ids"] == ["r-mcp"] for r in roles)
    owners = [
        b
        for m, p, b in fake.writes
        if p == "/api/v1/data-types/t-new/acl" and b["level"] == "OWNER"
    ]
    assert [o["subjects"][0]["id"] for o in owners] == [ADMIN, "d-ops"]
    # ② 编码级授权按原范围搬；单条授权按服务名对上；旧数据清掉、旧编码删掉
    assert ("POST", "/api/v1/data/o-new-serpapi/acl") in writes
    assert ("DELETE", "/open/v1/data-permissions") in writes
    assert writes[-1] == ("DELETE", "/api/v1/data-types/t-old")
    moved_type = [
        b for m, p, b in fake.writes if p == "/api/v1/data-types/t-new/acl" and b["level"] == "READ"
    ]
    assert moved_type == [
        {"level": "READ", "subjects": [{"type": "dept", "id": "d-root"}], "include_sub": True}
    ]


def test_old_data_code_is_kept_while_services_are_missing_in_the_gateway() -> None:
    fake = FakeUCAdmin(targets=())
    keep = reg._retire_mcp(_uc(fake), "atlas", OPS)
    # 操作码照常退役（持有人已经搬了）；数据编码先保留，等服务在网关登记后再跑一次
    assert keep == set()
    assert ("DELETE", "/api/v1/data-types/t-old") not in [(m, p) for m, p, _ in fake.writes]
