"""uc 模式：身份来自请求头（网关写入 X-Uid），操作码按 X-Auth-Uid 取用户中心授权快照
（内网身份传递 §3、§6），按操作码鉴权。

★ 用户中心用 httpx.MockTransport 模拟，不碰真实服务。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from atlas_config import auth
from atlas_config.api.app import create_app
from atlas_config.settings import get_settings

SNAPSHOTS = {
    "u-reviewer": {"can_access": True, "permissions": ["skill:review", "mcp:manage"]},
    "u-member": {"can_access": True, "permissions": ["chat:use"]},
    "u-outsider": {"can_access": False, "permissions": []},
}


@pytest.fixture
def uc_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str | None]]]:
    calls: list[dict[str, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        # 内网免令牌：以 Atlas 应用的身份调用，透传本次请求的身份
        assert "authorization" not in req.headers
        assert req.headers["x-caller-app"] == "atlas"
        uid = req.url.path.split("/")[4]
        calls.append({"auth_uid": uid, "x-tenant-id": req.headers.get("x-tenant-id")})
        body = SNAPSHOTS.get(uid)
        return httpx.Response(200, json=body) if body else httpx.Response(404, json={})

    monkeypatch.setenv("ATLAS_CONFIG_AUTH_MODE", "uc")
    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())
    get_settings.cache_clear()
    yield calls
    get_settings.cache_clear()


@pytest.fixture
async def client(storage, uc_mode) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(storage=storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cfg"
    ) as c:
        yield c


async def test_uc_identity_headers_and_operations(
    client: httpx.AsyncClient, uc_mode: list[dict[str, str | None]]
) -> None:
    # 没有 X-Uid（没经过网关）：401；旧的 X-User-Id 不再认
    assert (await client.get("/config/mcp/servers")).status_code == 401
    r = await client.get("/config/mcp/servers", headers={"X-User-Id": "u-reviewer"})
    assert r.status_code == 401
    # 用户中心里没有这个人：401；不在 Atlas 的可访问范围：403
    assert (await client.get("/config/mcp/servers", headers={"X-Uid": "ghost"})).status_code == 401
    r = await client.get("/config/mcp/servers", headers={"X-Uid": "u-outsider"})
    assert r.status_code == 403

    # 有 mcp:manage：能看；成员：403
    reviewer = {"X-Uid": "u-reviewer", "X-Tenant-Id": "t-1"}
    assert (await client.get("/config/mcp/servers", headers=reviewer)).status_code == 200
    assert uc_mode[-1] == {"auth_uid": "u-reviewer", "x-tenant-id": "t-1"}
    member = {"X-Uid": "u-member"}
    assert (await client.get("/config/mcp/servers", headers=member)).status_code == 403
    assert (await client.get("/config/audit", headers=member)).status_code == 403

    # 操作码按 X-Auth-Uid 判：成员在用、按审核人的权限
    on_behalf = {"X-Uid": "u-member", "X-Auth-Uid": "u-reviewer"}
    assert (await client.get("/config/mcp/servers", headers=on_behalf)).status_code == 200

    # 写请求不再要 CSRF 头（网关 + 用户中心统一校验）
    body = {"name": "x", "url": "http://mcp.local", "credential_scope": "platform"}
    r = await client.post("/config/mcp/servers", json=body, headers=reviewer)
    assert r.status_code == 201, r.text

    # 快照有缓存：同一个人只问一次用户中心
    assert sum(1 for c in uc_mode if c["auth_uid"] == "u-reviewer") == 1
