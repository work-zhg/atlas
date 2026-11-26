"""平台服务与身份凭据（c4-model/mcpgateway/code-catalog.html、code-invocation.html）。

平台服务    运维命令行登记一次（例如知识库），所有租户可见；租户看得到、改不了，也不能建同名服务；
           都是公开的；上游地址是平台自己写的，不查地址
身份凭据    Atlas 打开 CLI 会话时签发（X-Identity-Grant）：网关验签后，给上游写按谁判、谁在用、
           会话；签名不对、租户和 Key 的对不上一律 401（grant_invalid）
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_mcpgateway.errors import Conflict, Invalid
from atlas_mcpgateway.invocation.grant import sign
from fake_upstreams import REST_KEY, FakeRest
from gateway_testkit import (
    ADMIN,
    ALICE,
    BOB,
    CAROL,
    OTHER_TENANT,
    TEST_TENANT,
    FakePermissions,
    apply_user_key,
    as_user,
    in_tenant,
    mcp_client,
)

GRANT_SECRET = "grant-secret-for-tests"


@pytest.fixture
def grant_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from atlas_mcpgateway.settings import get_settings

    monkeypatch.setenv("ATLAS_MCPGW_IDENTITY_GRANT_SECRET", GRANT_SECRET)
    get_settings.cache_clear()
    yield GRANT_SECRET
    get_settings.cache_clear()


@pytest.fixture
async def app(grant_secret: str, perms: FakePermissions) -> AsyncIterator[Any]:
    """本文件的网关配上身份凭据的密钥（覆盖 conftest 的 app）。"""
    from atlas_mcpgateway.api.app import create_app

    application = create_app(uc=perms)
    async with application.router.lifespan_context(application):
        yield application


def _platform_spec(base: str, name: str = "kb", **over: Any) -> dict[str, Any]:
    return {
        "kind": "rest",
        "name": name,
        "display_name": "知识库",
        "description": "平台的知识库",
        "connection": {"base_url": f"{base}/rest/api", "spec_url": ""},
        "credential": {"type": "apikey", "where": "header", "key": "X-Api-Key", "secret": REST_KEY},
        "tools": [
            {
                "name": "get_customer",
                "description": "按 id 查客户",
                "effect": "read",
                "mapping": {
                    "method": "GET",
                    "path": "/customers/{id}",
                    "params": [{"name": "id", "in": "path", "type": "string", "required": True}],
                },
            }
        ],
        **over,
    }


async def _register_platform(spec: dict[str, Any], settings: Any = None) -> Any:
    """运维命令行（service --platform add）做的事：不在任何租户里。"""
    from atlas_mcpgateway import wiring
    from atlas_mcpgateway.__main__ import add_service
    from atlas_mcpgateway.db.session import get_sessionmaker
    from atlas_mcpgateway.settings import get_settings
    from atlas_mcpgateway.upstream.gateway import UpstreamGateway

    settings = settings or get_settings()
    upstream = UpstreamGateway(settings)
    try:
        async with get_sessionmaker()() as s:
            catalog = wiring.catalog(s, settings, FakePermissions(), upstream, platform=True)
            row = await add_service(catalog, spec, owner="ops-cli")
            await s.commit()
            return row
    finally:
        await upstream.aclose()


async def test_a_platform_service_is_visible_to_every_tenant_and_read_only(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    row = await _register_platform(_platform_spec(upstream_base))
    assert row.platform and row.visibility == "public"

    for headers in (as_user(ADMIN), in_tenant(OTHER_TENANT, **as_user(ADMIN))):
        listed = (await client.get("/api/v1/services", headers=headers)).json()
        kb = next(s for s in listed if s["name"] == "kb")
        assert kb["platform"] is True and kb["tool_count"] == 1
    detail = (await client.get("/api/v1/services/kb", headers=as_user(ADMIN))).json()
    assert detail["platform"] is True and detail["can_manage"] is False
    # 租户的管理员改不了，也不能建同名的服务
    r = await client.patch("/api/v1/services/kb", json={"description": "x"}, headers=as_user(ADMIN))
    assert r.status_code == 403 and "平台服务" in r.json()["detail"]
    r = await client.post("/api/v1/services/kb/disable", headers=as_user(ADMIN))
    assert r.status_code == 403
    body = {**_platform_spec(upstream_base), "visibility": "public", "selected": []}
    body.pop("tools")
    r = await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))
    assert r.status_code == 409 and "平台服务" in r.json()["detail"]
    # 再登记一次同名的平台服务：已存在就跳过（部署脚本可以重复跑）
    assert await _register_platform(_platform_spec(upstream_base)) is None


async def test_platform_names_are_reserved_and_only_rest_or_grpc(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    body = {
        "kind": "rest",
        "name": "crm",
        "visibility": "public",
        "connection": {"base_url": f"{upstream_base}/rest/api", "spec_url": ""},
        "credential": {"type": "none"},
        "selected": [],
    }
    assert (
        await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))
    ).status_code == 201
    # 哪个租户已经用了这个名字：平台服务不能再用（名字全平台保留）
    with pytest.raises(Conflict):
        await _register_platform(_platform_spec(upstream_base, name="crm"))
    with pytest.raises(Invalid, match="平台服务只能是 REST、gRPC"):
        await _register_platform(
            {
                "kind": "mcp",
                "name": "proxy",
                "connection": {"url": f"{upstream_base}/mcp/x"},
                "credential": {"type": "none"},
            }
        )


def _signed(thread: str, auth_uid: str, uid: str, tenant: str = TEST_TENANT) -> str:
    return sign(GRANT_SECRET, thread=thread, auth_uid=auth_uid, uid=uid, tenant=UUID(tenant))


async def test_calls_carry_the_identity_grant_to_the_upstream(
    app: Any, client: httpx.AsyncClient, fake_rest: FakeRest, upstream_base: str
) -> None:
    await _register_platform(_platform_spec(upstream_base))
    key = await apply_user_key(client, ALICE)

    # 没带身份凭据：按 Key 的主体；X-Tenant-Id 是 Key 所在的租户
    async with mcp_client(app, "kb", key["secret"]) as c:
        assert [t.name for t in await c.list_tools()] == ["get_customer"]
        assert (await c.call_tool_mcp("get_customer", {"id": "C1"})).is_error is False
    headers = fake_rest.seen[-1][3]
    assert headers["x-auth-uid"] == key["subject_uid"] and headers["x-uid"] == ALICE
    assert headers["x-tenant-id"] == TEST_TENANT
    assert headers["x-api-key"] == REST_KEY

    # 带了：按谁判、谁在用、会话都以凭据为准（调用方自己写的 X-Auth-Uid 不认）
    grant = _signed("thread-9", BOB, CAROL)
    async with mcp_client(
        app, "kb", key["secret"], **{"X-Identity-Grant": grant, "X-Auth-Uid": ADMIN}
    ) as c:
        assert (await c.call_tool_mcp("get_customer", {"id": "C2"})).is_error is False
    headers = fake_rest.seen[-1][3]
    assert (headers["x-auth-uid"], headers["x-uid"]) == (BOB, CAROL)
    assert headers["x-thread-id"] == "thread-9" and headers["x-tenant-id"] == TEST_TENANT

    # 别的租户的 Key 同样能用平台服务
    theirs = await client.post("/api/v1/keys/me", headers=in_tenant(OTHER_TENANT, **as_user(BOB)))
    async with mcp_client(app, "kb", theirs.json()["secret"]) as c:
        assert (await c.call_tool_mcp("get_customer", {"id": "C3"})).is_error is False
    assert fake_rest.seen[-1][3]["x-tenant-id"] == OTHER_TENANT


async def test_a_bad_identity_grant_is_rejected(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    await _register_platform(_platform_spec(upstream_base))
    key = await apply_user_key(client, ALICE)

    async def tools_list(grant: str) -> httpx.Response:
        return await client.post(
            "/mcp/kb",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {key['secret']}",
                "Accept": "application/json, text/event-stream",
                "X-Identity-Grant": grant,
            },
        )

    forged = sign("wrong-secret", thread="t", auth_uid=BOB, uid=BOB, tenant=UUID(TEST_TENANT))
    for bad in (forged, "not-a-grant", _signed("t", BOB, BOB, tenant=OTHER_TENANT)):
        r = await tools_list(bad)
        assert r.status_code == 401, r.text
        assert r.json()["error"]["data"]["reason"] == "grant_invalid"
        assert r.json()["error"]["code"] == -32047


async def test_platform_upstreams_skip_the_address_check() -> None:
    """平台服务的地址是部署时平台自己写的：不查地址；租户填的照样不能指向平台内部。"""
    from atlas_mcpgateway.settings import get_settings
    from atlas_mcpgateway.upstream.gateway import UpstreamGateway
    from atlas_mcpgateway.upstream.guard import UpstreamBlocked
    from atlas_mcpgateway.upstream.spec import UpstreamSpec

    settings = get_settings().model_copy(update={"upstream_allow_cidrs": []})
    upstream = UpstreamGateway(settings)
    connection = {"base_url": "http://127.0.0.1:8050", "spec_url": ""}
    try:
        with pytest.raises(UpstreamBlocked):
            await upstream.check_addresses(UpstreamSpec("rest", connection))
        await upstream.check_addresses(UpstreamSpec("rest", connection, platform=True))
    finally:
        await upstream.aclose()


async def test_registering_a_platform_service_at_an_internal_address(
    client: httpx.AsyncClient,
) -> None:
    """运维命令行登记平台服务（如集群内的知识库）：登记这一步也不查地址，只有租户填的才查。"""
    from atlas_mcpgateway.settings import get_settings

    strict = get_settings().model_copy(update={"upstream_allow_cidrs": []})
    row = await _register_platform(_platform_spec("http://127.0.0.1:9", name="kb-internal"), strict)
    assert row.platform and row.connection["base_url"] == "http://127.0.0.1:9/rest/api"
