"""uc 模式：浏览器直连配置服务，会话交给 atlas-server 校验（权限设计 §9），按操作码鉴权。

★ atlas-server 用 httpx.MockTransport 模拟，不碰真实服务。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from atlas_config import auth
from atlas_config.api.app import create_app
from atlas_config.settings import get_settings

SESSIONS = {
    "reviewer-token": {"user_id": "u-reviewer", "operations": ["skill:review", "mcp:manage"]},
    "member-token": {"user_id": "u-member", "operations": ["chat:use"]},
}


@pytest.fixture
def uc_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/internal/v1/auth/introspect"
        assert req.headers["X-Internal-Token"] == "shared-secret"
        token = json.loads(req.content)["token"]
        calls.append(token)
        body = SESSIONS.get(token)
        return httpx.Response(200, json=body) if body else httpx.Response(401, json={})

    monkeypatch.setenv("ATLAS_CONFIG_AUTH_MODE", "uc")
    monkeypatch.setenv("ATLAS_CONFIG_INTERNAL_TOKEN", "shared-secret")
    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._IntrospectCache())
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


async def test_uc_session_and_operations(client: httpx.AsyncClient, uc_mode: list[str]) -> None:
    # 没有登录：401（X-User-Id 在 uc 模式无效）
    r = await client.get("/config/mcp/servers", headers={"X-User-Id": "admin"})
    assert r.status_code == 401
    # 伪造的会话：401
    client.cookies.set("atlas_session", "forged")
    assert (await client.get("/config/mcp/servers")).status_code == 401

    # 有 mcp:manage：能看；成员：403
    client.cookies.set("atlas_session", "reviewer-token")
    assert (await client.get("/config/mcp/servers")).status_code == 200
    client.cookies.set("atlas_session", "member-token")
    assert (await client.get("/config/mcp/servers")).status_code == 403
    assert (await client.get("/config/audit")).status_code == 403

    # 写请求必须带 CSRF 双提交
    client.cookies.set("atlas_session", "reviewer-token")
    body = {"name": "x", "url": "http://mcp.local", "credential_scope": "platform"}
    r = await client.post("/config/mcp/servers", json=body)
    assert r.status_code == 403 and "校验" in r.json()["detail"]

    # 校验结果有缓存：同一个令牌只问一次 atlas-server
    assert uc_mode.count("reviewer-token") == 1
