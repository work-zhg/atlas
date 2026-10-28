"""模型：登记、合并列表、公有 / 私有、纳入平台、编辑、重新登记、删除（方案第 3、4 章）。"""

from __future__ import annotations

from typing import Any

import httpx
from modelgw_testkit import ADMIN, ALICE, BOB, DEPT, FakeLiteLLM, FakePermissions, as_user

H = as_user(ADMIN)


def model_body(name: str = "deepseek-chat", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "display_name": "DeepSeek V3.2",
        "provider": "deepseek",
        "visibility": "public",
        "api_key": "sk-provider",
        "max_input_tokens": 131072,
        "max_output_tokens": 8192,
        "input_price": 0.28,
        "output_price": 0.42,
    }
    body.update(extra)
    return body


async def register(client: httpx.AsyncClient, name: str = "deepseek-chat", **extra: Any) -> dict:
    r = await client.post("/api/v1/models", json=model_body(name, **extra), headers=H)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def listing(client: httpx.AsyncClient, uid: str = ADMIN) -> dict[str, dict]:
    r = await client.get("/api/v1/models", headers=as_user(uid))
    assert r.status_code == 200, r.text
    return {m["name"]: m for m in r.json()["data"]}


async def test_register_goes_to_litellm_first_with_provider_and_specs(
    client: httpx.AsyncClient, litellm: FakeLiteLLM, perms: FakePermissions
) -> None:
    out = await register(client)

    deployment = litellm.models[str(out["uuid"])]
    assert deployment["model_name"] == "deepseek-chat"
    params = deployment["litellm_params"]
    assert params["model"] == "deepseek/deepseek-chat"
    assert params["custom_llm_provider"] == "deepseek" and params["api_key"] == "sk-provider"
    # 价格放 litellm_params（放 model_info 不生效），规格放 model_info
    assert abs(params["input_cost_per_token"] - 0.28e-6) < 1e-15
    assert deployment["model_info"]["max_input_tokens"] == 131072
    # 平台只落基本信息：返回里的规格价格来自 LiteLLM
    assert out["status"] == "ok" and out["source"] == "platform" and out["managed"] is True
    assert out["input_price"] == 0.28 and out["max_output_tokens"] == 8192
    # 用户中心登记了数据对象（登记人是所有者），公有模型不写「使用」授权
    assert [r[1] for r in perms.acls["deepseek-chat"]] == ["OWNER"]


async def test_private_model_registered_with_grants(
    client: httpx.AsyncClient, perms: FakePermissions
) -> None:
    await register(
        client,
        "claude-sonnet-5",
        provider="anthropic",
        visibility="private",
        grants=[{"subject_type": "dept", "subject_id": DEPT, "include_sub": True}],
    )
    assert ("READ", "dept", DEPT) in [(r[1], r[2], r[3]) for r in perms.acls["claude-sonnet-5"]]
    row = (await listing(client))["claude-sonnet-5"]
    assert [g["subject_id"] for g in row["grants"]] == [DEPT]


async def test_register_rejects_duplicates_including_litellm_direct(
    client: httpx.AsyncClient, litellm: FakeLiteLLM
) -> None:
    await register(client)
    again = await client.post("/api/v1/models", json=model_body(), headers=H)
    assert again.status_code == 409

    litellm.add_config_model("qwen-plus")
    direct = await client.post(
        "/api/v1/models", json=model_body("qwen-plus", provider="dashscope"), headers=H
    )
    assert direct.status_code == 409
    assert "纳入平台" in direct.json()["detail"]


async def test_register_validates_provider_route_and_name(client: httpx.AsyncClient) -> None:
    bad_name = await client.post("/api/v1/models", json=model_body("bad name"), headers=H)
    assert bad_name.status_code == 400
    no_key = await client.post("/api/v1/models", json=model_body(api_key=None), headers=H)
    assert no_key.status_code == 400
    # vLLM 自部署：必须有地址，可以没有凭据
    no_base = await client.post(
        "/api/v1/models",
        json=model_body("bge-m3", provider="hosted_vllm", api_key=None),
        headers=H,
    )
    assert no_base.status_code == 400
    ok = await client.post(
        "/api/v1/models",
        json=model_body(
            "bge-m3", provider="hosted_vllm", api_key=None, api_base="http://vllm:8000/v1"
        ),
        headers=H,
    )
    assert ok.status_code == 201, ok.text


async def test_register_undoes_litellm_when_user_center_fails(
    client: httpx.AsyncClient, litellm: FakeLiteLLM, perms: FakePermissions
) -> None:
    perms.down = True
    r = await client.post("/api/v1/models", json=model_body(), headers=H)
    assert r.status_code == 503
    assert litellm.models == {}, "用户中心失败时要撤回 LiteLLM 里的登记"
    perms.down = False
    assert await listing(client) == {}


async def test_only_admins_manage_models(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/models", json=model_body(), headers=as_user(ALICE))
    assert r.status_code == 403
    me = await client.get("/api/v1/me", headers=as_user(ALICE))
    assert me.json()["can_manage"] is False


async def test_listing_merges_platform_missing_and_litellm_direct(
    client: httpx.AsyncClient, litellm: FakeLiteLLM
) -> None:
    await register(client)
    await register(client, "kimi-k2", provider="moonshot")
    litellm.drop_model_named("kimi-k2")
    litellm.add_config_model("qwen-plus", max_input_tokens=131072)

    rows = await listing(client)
    assert rows["deepseek-chat"]["status"] == "ok"
    assert rows["kimi-k2"]["status"] == "missing"
    direct = rows["qwen-plus"]
    assert direct["source"] == "litellm" and direct["origin"] == "config"
    assert direct["uuid"] is None and direct["max_input_tokens"] == 131072


async def test_users_see_public_and_granted_private_models(
    client: httpx.AsyncClient, perms: FakePermissions, litellm: FakeLiteLLM
) -> None:
    await register(client, "deepseek-chat")
    await register(client, "claude-sonnet-5", provider="anthropic", visibility="private")
    await register(
        client,
        "claude-haiku",
        provider="anthropic",
        visibility="private",
        grants=[{"subject_type": "dept", "subject_id": DEPT}],
    )
    litellm.add_config_model("qwen-plus")

    alice = await listing(client, ALICE)
    assert set(alice) == {"deepseek-chat", "claude-haiku"}
    assert alice["deepseek-chat"]["access"] == "public"
    assert alice["claude-haiku"]["access"] == "granted"
    assert alice["claude-haiku"]["api_base"] is None, "普通用户看不到地址"
    assert set(await listing(client, BOB)) == {"deepseek-chat"}


async def test_adopt_litellm_direct_model_without_touching_litellm(
    client: httpx.AsyncClient, litellm: FakeLiteLLM, perms: FakePermissions
) -> None:
    model_id = litellm.add_config_model("qwen-plus", provider="dashscope")
    before = dict(litellm.models)
    r = await client.post(
        "/api/v1/models/adopt",
        json={"name": "qwen-plus", "display_name": "通义千问 Plus", "visibility": "public"},
        headers=H,
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["source"] == "platform" and out["managed"] is False
    assert out["provider"] == "dashscope"
    assert litellm.models == before and model_id in litellm.models
    assert "qwen-plus" in perms.acls


async def test_update_changes_litellm_and_platform_and_keeps_provider_key(
    client: httpx.AsyncClient, litellm: FakeLiteLLM
) -> None:
    out = await register(client)
    r = await client.put(
        f"/api/v1/models/{out['uuid']}",
        json={
            "display_name": "DeepSeek 新名字",
            "provider": "deepseek",
            "visibility": "private",
            "description": "改了",
            "api_base": "https://api.deepseek.com",
            "max_input_tokens": 65536,
        },
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "DeepSeek 新名字" and r.json()["visibility"] == "private"
    params = litellm.models[str(out["uuid"])]["litellm_params"]
    assert params["api_key"] == "sk-provider", "凭据留空 = 不改"
    assert params["api_base"] == "https://api.deepseek.com"
    assert litellm.models[str(out["uuid"])]["model_info"]["max_input_tokens"] == 65536


async def test_reregister_missing_model_rebuilds_deployment(
    client: httpx.AsyncClient, litellm: FakeLiteLLM
) -> None:
    out = await register(client, "kimi-k2", provider="moonshot")
    not_missing = await client.post(
        f"/api/v1/models/{out['uuid']}/reregister",
        json={"provider": "moonshot", "api_key": "sk-new"},
        headers=H,
    )
    assert not_missing.status_code == 409

    litellm.drop_model_named("kimi-k2")
    r = await client.post(
        f"/api/v1/models/{out['uuid']}/reregister",
        json={"provider": "moonshot", "api_key": "sk-new"},
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert litellm.models[str(out["uuid"])]["litellm_params"]["api_key"] == "sk-new"


async def test_delete_removes_everywhere(
    client: httpx.AsyncClient, litellm: FakeLiteLLM, perms: FakePermissions
) -> None:
    out = await register(client)
    r = await client.delete(f"/api/v1/models/{out['uuid']}", headers=H)
    assert r.status_code == 200, r.text
    assert litellm.models == {}
    assert perms.removed == ["deepseek-chat"]
    assert await listing(client) == {}


async def test_test_connection(client: httpx.AsyncClient) -> None:
    ok = await client.post(
        "/api/v1/models/test-connection",
        json={"name": "deepseek-chat", "provider": "deepseek", "api_key": "sk-x"},
        headers=H,
    )
    assert ok.json() == {"ok": True, "message": "连接成功"}
    bad = await client.post(
        "/api/v1/models/test-connection",
        json={"name": "deepseek-chat", "provider": "deepseek", "api_key": "bad"},
        headers=H,
    )
    assert bad.json()["ok"] is False


async def test_grants_add_and_revoke(client: httpx.AsyncClient, perms: FakePermissions) -> None:
    out = await register(client, "claude-sonnet-5", provider="anthropic", visibility="private")
    r = await client.post(
        f"/api/v1/models/{out['uuid']}/grants",
        json=[{"subject_type": "user", "subject_id": ALICE}],
        headers=H,
    )
    assert r.status_code == 200, r.text
    alice_grant = next(g for g in r.json() if g["subject_id"] == ALICE)
    assert alice_grant["permission"] == "READ"
    revoke = await client.delete(
        f"/api/v1/models/{out['uuid']}/grants/{alice_grant['id']}", headers=H
    )
    assert revoke.status_code == 200
    assert ALICE not in [r[3] for r in perms.acls["claude-sonnet-5"]]

    public = await register(client, "deepseek-chat")
    refused = await client.post(
        f"/api/v1/models/{public['uuid']}/grants",
        json=[{"subject_type": "user", "subject_id": ALICE}],
        headers=H,
    )
    assert refused.status_code == 400


async def test_listing_survives_litellm_down(
    client: httpx.AsyncClient, litellm: FakeLiteLLM
) -> None:
    await register(client)
    litellm.down = True
    r = await client.get("/api/v1/models", headers=H)
    assert r.status_code == 200
    body = r.json()
    # 进程里有上一份：照常列出
    assert body["data"][0]["name"] == "deepseek-chat"
