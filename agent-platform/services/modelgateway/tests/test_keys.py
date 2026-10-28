"""API Key：用户 Key、客户端 Key、内部接口、同步到 LiteLLM、对账（方案第 5、6、7 章）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from modelgw_testkit import (
    ADMIN,
    ALICE,
    BOB,
    DEPT,
    GONE,
    FakeLiteLLM,
    FakePermissions,
    as_app,
    as_user,
)
from test_models import register

H = as_user(ADMIN)
AGENT = {"client_type": "Agent", "client_code": "agt_7f3a21", "name": "代码评审助手"}


@pytest.fixture
async def models(client: httpx.AsyncClient) -> dict[str, dict]:
    """公有 deepseek-chat、bge-m3；私有 claude-sonnet-5（只授给 ALICE 的部门）。"""
    return {
        "deepseek-chat": await register(client, "deepseek-chat"),
        "bge-m3": await register(
            client,
            "bge-m3",
            provider="hosted_vllm",
            api_key=None,
            api_base="http://vllm:8000/v1",
        ),
        "claude-sonnet-5": await register(
            client,
            "claude-sonnet-5",
            provider="anthropic",
            visibility="private",
            grants=[{"subject_type": "dept", "subject_id": DEPT}],
        ),
    }


async def apply(client: httpx.AsyncClient, uid: str, models: list[str]) -> httpx.Response:
    return await client.post("/api/v1/keys/me", json={"models": models}, headers=as_user(uid))


async def create_client_key(client: httpx.AsyncClient, **body: Any) -> dict:
    r = await client.post("/internal/client-keys", json={**AGENT, **body}, headers=as_app())
    assert r.status_code == 200, r.text
    return dict(r.json())


async def admin_client_key(client: httpx.AsyncClient, code: str = "agt_7f3a21") -> dict:
    r = await client.get("/api/v1/client-keys", params={"q": code}, headers=H)
    return dict(next(k for k in r.json() if k["client_code"] == code))


# ── 用户 Key


async def test_user_applies_key_with_public_and_granted_models(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    r = await apply(client, ALICE, ["deepseek-chat", "claude-sonnet-5"])
    assert r.status_code == 201, r.text
    out = r.json()
    secret = out["secret"]
    assert out["masked"] == f"sk-…{secret[-4:]}"
    assert out["effective"] == ["deepseek-chat", "claude-sonnet-5"]
    entry = litellm.key_of(secret)
    assert entry["models"] == ["deepseek-chat", "claude-sonnet-5"]
    assert entry["user_id"] == ALICE and entry["metadata"]["key_type"] == "user"

    # 原文只在申请时返回一次
    again = await client.get("/api/v1/keys/me", headers=as_user(ALICE))
    assert "secret" not in again.json() or again.json().get("secret") is None
    # 一人一把
    assert (await apply(client, ALICE, ["deepseek-chat"])).status_code == 409


async def test_user_cannot_pick_private_model_without_grant(
    client: httpx.AsyncClient, models: dict
) -> None:
    r = await apply(client, BOB, ["deepseek-chat", "claude-sonnet-5"])
    assert r.status_code == 400
    assert "claude-sonnet-5" in r.json()["detail"]
    assert (await apply(client, BOB, [])).status_code == 422


async def test_disabled_user_cannot_apply(client: httpx.AsyncClient, models: dict) -> None:
    assert (await apply(client, GONE, ["deepseek-chat"])).status_code == 400


async def test_revoking_grant_drops_model_from_user_key(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM, perms: FakePermissions
) -> None:
    secret = (await apply(client, ALICE, ["deepseek-chat", "claude-sonnet-5"])).json()["secret"]
    sonnet = models["claude-sonnet-5"]
    acl = next(g for g in perms.acls["claude-sonnet-5"] if g[2] == "dept")
    r = await client.delete(f"/api/v1/models/{sonnet['uuid']}/grants/{acl[0]}", headers=H)
    assert r.json()["keys_updated"] == 1

    assert litellm.key_of(secret)["models"] == ["deepseek-chat"]
    mine = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    states = {m["name"]: m["state"] for m in mine["models"]}
    assert states == {"deepseek-chat": "ok", "claude-sonnet-5": "revoked"}


async def test_key_with_no_effective_models_is_blocked_not_opened(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    secret = (await apply(client, ALICE, ["claude-sonnet-5"])).json()["secret"]
    # 撤掉私有模型的授权 → 用户 Key 里唯一的模型失效
    sonnet = models["claude-sonnet-5"]
    r = await client.get(f"/api/v1/models/{sonnet['uuid']}/grants", headers=H)
    for g in r.json():
        if g["permission"] == "READ":
            await client.delete(f"/api/v1/models/{sonnet['uuid']}/grants/{g['id']}", headers=H)

    entry = litellm.key_of(secret)
    assert entry["blocked"] is True, "有效模型为空：停用，而不是推空列表（= 全部模型）"
    assert entry["models"] == ["claude-sonnet-5"]
    mine = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert mine["no_models"] is True and mine["status"] == "active"


async def test_user_key_reset_block_unblock_delete(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    old = (await apply(client, ALICE, ["deepseek-chat"])).json()["secret"]
    reset = await client.post("/api/v1/keys/me/reset", headers=as_user(ALICE))
    new = reset.json()["secret"]
    assert (
        new != old
        and old not in litellm.keys
        and litellm.key_of(new)["models"] == ["deepseek-chat"]
    )

    await client.post("/api/v1/keys/me/block", headers=as_user(ALICE))
    assert litellm.key_of(new)["blocked"] is True
    await client.post("/api/v1/keys/me/unblock", headers=as_user(ALICE))
    assert litellm.key_of(new)["blocked"] is False

    changed = await client.put(
        "/api/v1/keys/me/models", json={"models": ["bge-m3"]}, headers=as_user(ALICE)
    )
    assert changed.status_code == 200, changed.text
    assert litellm.key_of(new)["models"] == ["bge-m3"]

    assert (await client.delete("/api/v1/keys/me", headers=as_user(ALICE))).status_code == 200
    assert litellm.keys == {}
    assert (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json() is None


# ── 客户端 Key


async def test_app_creates_client_key_pending_until_admin_assigns(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    created = await create_client_key(client)
    assert created["status"] == "pending_models" and created["key"] is None
    assert litellm.keys == {}, "待分配时不在 LiteLLM 生成 Key"

    # 幂等：重复创建返回同一把，名字跟着改
    again = await create_client_key(client, name="代码评审助手 2")
    assert again["name"] == "代码评审助手 2"

    key = await admin_client_key(client)
    assert key["status"] == "pending"
    # 客户端 Key 公有、私有都能分配
    r = await client.put(
        f"/api/v1/client-keys/{key['uuid']}/models",
        json={"models": ["deepseek-chat", "claude-sonnet-5"]},
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active" and "secret" not in r.json()

    got = await client.get(
        "/internal/client-keys",
        params={"client_type": "Agent", "client_code": "agt_7f3a21"},
        headers=as_app(),
    )
    body = got.json()
    assert body["status"] == "active"
    assert [m["name"] for m in body["models"]] == ["deepseek-chat", "claude-sonnet-5"]
    assert body["models"][0]["max_input_tokens"] == 131072
    entry = litellm.key_of(body["key"])
    assert entry["models"] == ["deepseek-chat", "claude-sonnet-5"]
    assert entry["metadata"]["client_type"] == "Agent"


async def test_client_key_rejects_unregistered_model(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    await create_client_key(client)
    litellm.add_config_model("qwen-plus")
    key = await admin_client_key(client)
    r = await client.put(
        f"/api/v1/client-keys/{key['uuid']}/models", json={"models": ["qwen-plus"]}, headers=H
    )
    assert r.status_code == 400


async def test_only_owning_app_and_admin_touch_client_keys(
    client: httpx.AsyncClient, models: dict
) -> None:
    other = await client.post("/internal/client-keys", json=AGENT, headers=as_app("teamflow"))
    assert other.status_code == 403
    missing = await client.post("/internal/client-keys", json=AGENT)
    assert missing.status_code == 403
    unknown = await client.post(
        "/internal/client-keys", json={**AGENT, "client_type": "Nope"}, headers=as_app()
    )
    assert unknown.status_code == 404
    await create_client_key(client)
    key = await admin_client_key(client)
    r = await client.put(
        f"/api/v1/client-keys/{key['uuid']}/models",
        json={"models": ["deepseek-chat"]},
        headers=as_user(ALICE),
    )
    assert r.status_code == 403


async def test_admin_creates_service_key_in_advance(
    client: httpx.AsyncClient, models: dict
) -> None:
    r = await client.post(
        "/api/v1/client-keys",
        json={"client_type": "Service", "client_code": "session-title", "name": "会话标题"},
        headers=H,
    )
    assert r.status_code == 201, r.text
    dup = await client.post(
        "/api/v1/client-keys",
        json={"client_type": "Service", "client_code": "session-title"},
        headers=H,
    )
    assert dup.status_code == 400
    await client.put(
        f"/api/v1/client-keys/{r.json()['uuid']}/models",
        json={"models": ["deepseek-chat"]},
        headers=H,
    )
    got = await client.get(
        "/internal/client-keys",
        params={"client_type": "Service", "client_code": "session-title"},
        headers=as_app(),
    )
    assert got.json()["status"] == "active" and got.json()["key"]


async def test_client_key_states_seen_by_app(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    await create_client_key(client)
    key = await admin_client_key(client)
    await client.put(
        f"/api/v1/client-keys/{key['uuid']}/models", json={"models": ["deepseek-chat"]}, headers=H
    )

    async def state() -> str:
        r = await client.get(
            "/internal/client-keys",
            params={"client_type": "Agent", "client_code": "agt_7f3a21"},
            headers=as_app(),
        )
        return str(r.json()["status"])

    await client.post(f"/api/v1/client-keys/{key['uuid']}/block", headers=H)
    assert await state() == "blocked"
    await client.post(f"/api/v1/client-keys/{key['uuid']}/unblock", headers=H)
    assert await state() == "active"

    litellm.drop_model_named("deepseek-chat")
    assert await state() == "no_models"


async def test_app_renames_and_deletes_client_key(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    await create_client_key(client)
    key = await admin_client_key(client)
    await client.put(
        f"/api/v1/client-keys/{key['uuid']}/models", json={"models": ["deepseek-chat"]}, headers=H
    )
    renamed = await client.patch(
        "/internal/client-keys", json={**AGENT, "name": "新名字"}, headers=as_app()
    )
    assert renamed.json()["name"] == "新名字"
    assert len(litellm.keys) == 1
    deleted = await client.delete(
        "/internal/client-keys",
        params={"client_type": "Agent", "client_code": "agt_7f3a21"},
        headers=as_app(),
    )
    assert deleted.json() == {"deleted": True}
    assert litellm.keys == {}


async def test_deleting_model_drops_it_from_client_keys(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    await create_client_key(client)
    key = await admin_client_key(client)
    await client.put(
        f"/api/v1/client-keys/{key['uuid']}/models",
        json={"models": ["deepseek-chat", "bge-m3"]},
        headers=H,
    )
    r = await client.delete(f"/api/v1/models/{models['bge-m3']['uuid']}", headers=H)
    assert r.json()["keys_updated"] == 1
    after = await admin_client_key(client)
    assert [m["name"] for m in after["models"]] == ["deepseek-chat"]
    secret = next(iter(litellm.keys))
    assert litellm.keys[secret]["models"] == ["deepseek-chat"]


async def test_internal_models_lists_callable_platform_models(
    client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM
) -> None:
    litellm.add_config_model("qwen-plus")
    litellm.drop_model_named("bge-m3")
    r = await client.get("/internal/models", headers=as_app())
    assert [m["name"] for m in r.json()["models"]] == ["claude-sonnet-5", "deepseek-chat"]


# ── 同步与对账


async def test_reconcile_repairs_drift_and_retries_failed_sync(
    app: Any, client: httpx.AsyncClient, models: dict, litellm: FakeLiteLLM, perms: FakePermissions
) -> None:
    from atlas_modelgateway.db.session import get_sessionmaker
    from atlas_modelgateway.reconcile import reconcile_once

    secret = (await apply(client, ALICE, ["deepseek-chat", "claude-sonnet-5"])).json()["secret"]
    # 有人在 LiteLLM 管理界面里改了这把 Key
    litellm.keys[secret]["models"] = ["deepseek-chat", "claude-sonnet-5", "qwen-plus"]
    # ALICE 调了部门：用户中心没有通知，只能靠对账发现
    perms.users[ALICE]["dept"] = None

    report = await reconcile_once(
        get_sessionmaker(),
        app.state.litellm,
        app.state.deployments,
        app.state.perms,
        app.state.subjects,
    )
    assert report.checked == 1 and report.changed == 1 and report.failed == 0
    assert litellm.key_of(secret)["models"] == ["deepseek-chat"]

    # 同步失败：记下原因，下次对账补上
    litellm.down = True
    await client.post("/api/v1/keys/me/block", headers=as_user(ALICE))
    litellm.down = False
    mine = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert mine["status"] == "blocked" and mine["sync_error"]
    assert litellm.key_of(secret)["blocked"] is False
    await reconcile_once(
        get_sessionmaker(),
        app.state.litellm,
        app.state.deployments,
        app.state.perms,
        app.state.subjects,
    )
    assert litellm.key_of(secret)["blocked"] is True
    mine = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert mine["sync_error"] is None
