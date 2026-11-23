"""多租户（c4-model/opscenter/design-tenant-isolation.html §8）。

隔离    服务名在租户内唯一；Key 说明了是哪个租户，别的租户的服务即使同名也找不到
身份    管理接口的人要在请求头写的租户里（用户中心核对）
地址    租户填的上游地址不能指向平台内部（upstream/guard.py）
停用    运营接口停用租户：管理接口 403、这个租户的 Key 在数据面一律无效；删除只删它的数据
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_mcpgateway.upstream.guard import AddressGuard, UpstreamBlocked
from fake_upstreams import REST_KEY
from gateway_testkit import (
    ADMIN,
    ALICE,
    BOB,
    OTHER_TENANT,
    TEST_TENANT,
    USERS_ELSEWHERE,
    apply_user_key,
    as_user,
    in_tenant,
    ops_request,
)


def _rest(base: str, name: str = "crm", **over: Any) -> dict[str, Any]:
    return {
        "kind": "rest",
        "name": name,
        "visibility": "public",
        "connection": {"base_url": f"{base}/rest/api", "spec_url": f"{base}/rest/openapi.json"},
        "credential": {"type": "apikey", "where": "header", "key": "X-Api-Key", "secret": REST_KEY},
        "selected": ["GET /customers/{id}"],
        **over,
    }


async def _tools_list(client: httpx.AsyncClient, service: str, secret: str) -> httpx.Response:
    return await client.post(
        f"/mcp/{service}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json, text/event-stream",
        },
    )


async def test_services_and_keys_stay_in_their_tenant(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    theirs = in_tenant(OTHER_TENANT, **as_user(ADMIN))
    r = await client.post("/api/v1/services", json=_rest(upstream_base), headers=as_user(ADMIN))
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/services", json=_rest(upstream_base, "only-a"), headers=as_user(ADMIN)
    )
    assert r.status_code == 201, r.text
    # 服务名只在租户内唯一：另一个租户也能登记 crm
    r = await client.post("/api/v1/services", json=_rest(upstream_base), headers=theirs)
    assert r.status_code == 201, r.text

    listed = (await client.get("/api/v1/services", headers=theirs)).json()
    assert [s["name"] for s in listed] == ["crm"]
    assert (await client.get("/api/v1/services/only-a", headers=theirs)).status_code == 404

    mine = await apply_user_key(client, ALICE)
    other_user = BOB  # 在另一个租户里
    r = await client.post("/api/v1/keys/me", headers=in_tenant(OTHER_TENANT, **as_user(other_user)))
    assert r.status_code == 201, r.text
    their_key = r.json()
    assert (await _tools_list(client, "only-a", mine["secret"])).status_code == 200
    r = await _tools_list(client, "only-a", their_key["secret"])
    assert r.status_code == 404, "别的租户的 Key 找不到这个租户的服务"
    assert r.json()["error"]["data"]["reason"] == "service_not_found"


async def test_a_user_cannot_claim_another_tenant(client: httpx.AsyncClient) -> None:
    USERS_ELSEWHERE[ALICE] = OTHER_TENANT
    assert (await client.get("/api/v1/services", headers=as_user(ALICE))).status_code == 401
    ok = await client.get("/api/v1/services", headers=in_tenant(OTHER_TENANT, **as_user(ALICE)))
    assert ok.status_code == 200
    assert (await client.get("/api/v1/services", headers={"X-Tenant-Id": ""})).status_code == 401


async def test_upstream_addresses_cannot_point_inside(client: httpx.AsyncClient) -> None:
    for base in ("http://169.254.169.254", "http://10.96.0.1:8080", "http://[fd00::1]"):
        body = _rest(base, name="inner")
        body["connection"]["spec_url"] = ""
        r = await client.post("/api/v1/services", json=body, headers=as_user(ADMIN))
        assert r.status_code == 400, (base, r.text)
        assert "内部网络" in r.json()["detail"]


async def test_address_guard_rules() -> None:
    guard = AddressGuard(["10.1.0.0/16"])
    await guard.check("https://8.8.8.8/mcp")
    await guard.check("10.1.2.3:443")  # 部署时放开的网段
    for bad in ("http://127.0.0.1:9", "localhost:50051", "dns:///127.0.0.1:1", "http://100.64.0.1"):
        with pytest.raises(UpstreamBlocked):
            await guard.check(bad)


async def test_suspend_shuts_out_admins_and_keys_and_purge_deletes_only_this_tenant(
    client: httpx.AsyncClient, upstream_base: str
) -> None:
    theirs = in_tenant(OTHER_TENANT, **as_user(ADMIN))
    assert (
        await client.post("/api/v1/services", json=_rest(upstream_base), headers=as_user(ADMIN))
    ).status_code == 201
    assert (
        await client.post("/api/v1/services", json=_rest(upstream_base), headers=theirs)
    ).status_code == 201
    key = await apply_user_key(client, ALICE)

    def ops(action: str) -> dict[str, Any]:
        return ops_request("POST", f"/ops/v1/tenants/{TEST_TENANT}/{action}")

    path = f"/ops/v1/tenants/{TEST_TENANT}"
    assert (await client.post(f"{path}/suspend", **ops("suspend"))).status_code == 200
    assert (await client.get("/api/v1/services", headers=as_user(ADMIN))).status_code == 403
    r = await _tools_list(client, "crm", key["secret"])
    assert r.status_code == 401 and r.json()["error"]["data"]["reason"] == "key_invalid"

    r = await client.post(f"{path}/purge", **ops("purge"))
    assert r.status_code == 200 and r.json()["remaining"] is False
    assert r.json()["deleted"]["mcp_service"] == 1 and r.json()["deleted"]["mcp_api_key"] == 1
    listed = (await client.get("/api/v1/services", headers=theirs)).json()
    assert [s["name"] for s in listed] == ["crm"], "别的租户的不动"
