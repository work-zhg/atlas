"""管理平台：账号、模型和线路、Key、授权、内部接口、调用记录、租户；最后接上网关走一遍。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from gw_testkit import FakeUpstream, anthropic_json, error
from modelgw_testkit import (
    ADMIN,
    ALICE,
    BOB,
    DEPT,
    OTHER_TENANT,
    FakePermissions,
    as_app,
    as_user,
)

A = as_user(ADMIN)


async def _account(
    client: httpx.AsyncClient, name: str = "公司账号", host: str = "a.test", **extra: Any
) -> dict[str, Any]:
    body = {
        "name": name,
        "provider": extra.pop("provider", "deepseek"),
        "endpoints": extra.pop("endpoints", {"anthropic": f"https://{host}/anthropic"}),
        "secret": "sk-upstream-1234",
        **extra,
    }
    r = await client.post("/api/v1/accounts", headers=A, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _model(
    client: httpx.AsyncClient, name: str, routes: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/models", headers=A, json={"name": name, "routes": routes, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── 账号 ───────────────────────────────────────────────────────────────


async def test_account_crud_and_validation(client: httpx.AsyncClient) -> None:
    acc = await _account(client)
    assert (
        acc["secret_masked"] == "…1234"
        and acc["status"] == "ok"
        and acc["provider_name"] == "DeepSeek"
    )
    # 密钥只写不读
    r = await client.get(f"/api/v1/accounts/{acc['id']}", headers=A)
    assert "secret" not in r.json()
    # 供应商不提供的格式
    r = await client.post(
        "/api/v1/accounts",
        headers=A,
        json={
            "name": "x",
            "provider": "siliconflow",
            "endpoints": {"anthropic": "https://x"},
            "secret": "k",
        },
    )
    assert r.status_code == 400 and "不提供" in r.json()["detail"]
    # 普通用户不能看账号
    assert (await client.get("/api/v1/accounts", headers=as_user(ALICE))).status_code == 403
    # 改名、改地址、不改密钥
    upd = {
        **{k: acc[k] for k in ("name", "provider", "endpoints", "egress")},
        "name": "主账号",
        "remark": "财务每月充值",
    }
    r = await client.put(f"/api/v1/accounts/{acc['id']}", headers=A, json=upd)
    assert r.json()["name"] == "主账号" and r.json()["secret_masked"] == "…1234"
    # 有线路在用不能删
    await _model(client, "m1", [{"account_id": acc["id"], "upstream_model": "deepseek-v4-pro"}])
    r = await client.delete(f"/api/v1/accounts/{acc['id']}", headers=A)
    assert r.status_code == 409


async def test_account_probe(client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    acc = await _account(client, provider="custom_anthropic")
    upstream.on("a.test", anthropic_json("hi"))
    r = await client.post(
        f"/api/v1/accounts/{acc['id']}/test", headers=A, json={"upstream_model": "claude-x"}
    )
    assert r.json()["ok"] is True
    sent = upstream.hits("a.test")[0]
    assert (
        sent.body["max_tokens"] == 1 and sent.headers["authorization"] == "Bearer sk-upstream-1234"
    )
    upstream.script["a.test"] = [
        error(
            401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
        )
    ]
    r = await client.post(
        f"/api/v1/accounts/{acc['id']}/test", headers=A, json={"upstream_model": "claude-x"}
    )
    assert r.json()["ok"] is False and "auth_failed" in r.json()["steps"][0]["message"]
    r = await client.get(f"/api/v1/accounts/{acc['id']}", headers=A)
    assert r.json()["check_error"]


# ── 模型和线路 ─────────────────────────────────────────────────────────


async def test_model_routes_and_caps(client: httpx.AsyncClient) -> None:
    a = await _account(client, "A")
    b = await _account(
        client, "B", "b.test", provider="anthropic", endpoints={"anthropic": "https://b.test"}
    )
    # 不传能力：按第一条线路的预置带出
    m = await _model(
        client,
        "claude",
        [{"account_id": b["id"], "upstream_model": "claude-opus-5"}],
        visibility="public",
        price={"input": "4", "cache_hit": "0.4", "output": "16"},
    )
    assert m["caps"]["vision"] is True and m["caps"]["context_window"] == 1_000_000
    assert m["status"] == "available" and m["routes"][0]["account_name"] == "B"
    # 承诺能看图，挂一条不能看图的备用线路（DeepSeek 预置不能看图）→ 拒绝
    upd = {
        "display_name": "Claude",
        "visibility": "public",
        "caps": m["caps"],
        "price": m["price"],
        "routes": [
            {"account_id": b["id"], "upstream_model": "claude-opus-5"},
            {"account_id": a["id"], "upstream_model": "deepseek-v4-pro", "priority": 2},
        ],
    }
    r = await client.put(f"/api/v1/models/{m['id']}", headers=A, json=upd)
    assert r.status_code == 400 and "看图" in r.json()["detail"]
    # 同一账号同一上游模型出现两次
    upd["routes"] = [{"account_id": b["id"], "upstream_model": "claude-opus-5"}] * 2
    r = await client.put(f"/api/v1/models/{m['id']}", headers=A, json=upd)
    assert r.status_code == 400 and "两次" in r.json()["detail"]
    # 带出默认能力
    r = await client.get(
        "/api/v1/models/caps-suggest",
        headers=A,
        params={"account_id": a["id"], "upstream_model": "deepseek-v4-pro"},
    )
    assert r.json()["preset"] is True and r.json()["caps"]["tools"] is True
    # 名字租户内唯一、不合法的名字
    r = await client.post(
        "/api/v1/models", headers=A, json={"name": "claude", "routes": [{"account_id": a["id"]}]}
    )
    assert r.status_code == 409
    r = await client.post(
        "/api/v1/models", headers=A, json={"name": "坏 名字", "routes": [{"account_id": a["id"]}]}
    )
    assert r.status_code == 400


async def test_private_model_grants_and_key_refresh(
    client: httpx.AsyncClient, perms: FakePermissions
) -> None:
    a = await _account(client)
    pub = await _model(client, "pub", [{"account_id": a["id"]}], visibility="public")
    priv = await _model(client, "priv", [{"account_id": a["id"]}], visibility="private")
    # BOB 看不到私有模型，也选不了
    names = [m["name"] for m in (await client.get("/api/v1/models", headers=as_user(BOB))).json()]
    assert names == ["pub"]
    r = await client.post(
        "/api/v1/keys/me", headers=as_user(BOB), json={"label": "Cursor", "models": ["pub", "priv"]}
    )
    assert r.status_code == 400
    # 授权给部门：ALICE 在部门里
    r = await client.post(
        f"/api/v1/models/{priv['id']}/grants",
        headers=A,
        json=[{"subject_type": "dept", "subject_id": DEPT}],
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/keys/me",
        headers=as_user(ALICE),
        json={"label": "Cursor", "models": ["pub", "priv"]},
    )
    assert r.status_code == 201
    key = r.json()
    assert key["secret"].startswith("sk-") and key["masked"].endswith(key["secret"][-4:])
    assert [m["state"] for m in key["models"]] == ["ok", "ok"]
    # 收回授权：ALICE 的 Key 里这个模型划掉
    acl = next(
        g
        for g in (await client.get(f"/api/v1/models/{priv['id']}/grants", headers=A)).json()
        if g["subject_type"] == "dept"
    )
    await client.delete(f"/api/v1/models/{priv['id']}/grants/{acl['id']}", headers=A)
    [mine] = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert {m["name"]: m["state"] for m in mine["models"]} == {"pub": "ok", "priv": "revoked"}
    # 停用模型：Key 里保留、标停用
    await client.post(f"/api/v1/models/{pub['id']}/disable", headers=A)
    [mine] = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert {m["name"]: m["state"] for m in mine["models"]}["pub"] == "disabled"
    # 删除模型：从 Key 里去掉
    await client.delete(f"/api/v1/models/{pub['id']}", headers=A)
    [mine] = (await client.get("/api/v1/keys/me", headers=as_user(ALICE))).json()
    assert [m["name"] for m in mine["models"]] == ["priv"]
    assert "pub" in perms.removed


# ── Key ────────────────────────────────────────────────────────────────


async def test_user_keys_many_and_limits(client: httpx.AsyncClient) -> None:
    a = await _account(client)
    await _model(client, "m1", [{"account_id": a["id"]}], visibility="public")
    k1 = (
        await client.post(
            "/api/v1/keys/me", headers=as_user(BOB), json={"label": "Cursor", "models": ["m1"]}
        )
    ).json()
    k2 = (
        await client.post(
            "/api/v1/keys/me",
            headers=as_user(BOB),
            json={"label": "脚本", "models": ["m1"], "rpm": 10},
        )
    ).json()
    assert len((await client.get("/api/v1/keys/me", headers=as_user(BOB))).json()) == 2
    # 改限额：只改传了的字段
    r = await client.put(
        f"/api/v1/keys/me/{k2['id']}", headers=as_user(BOB), json={"monthly_budget": "50"}
    )
    assert r.json()["rpm"] == 10 and Decimal(r.json()["monthly_budget"]) == 50
    # 别人的 Key 碰不到
    assert (
        await client.post(f"/api/v1/keys/me/{k1['id']}/block", headers=as_user(ALICE))
    ).status_code == 404
    # 管理员停用要写原因；本人不能自己解开管理员停用的
    await client.post(
        f"/api/v1/keys/{k1['id']}/block", headers=A, json={"reason": "Key 出现在公开仓库里"}
    )
    r = await client.post(f"/api/v1/keys/me/{k1['id']}/unblock", headers=as_user(BOB))
    assert r.status_code == 403
    [first] = [
        k
        for k in (await client.get("/api/v1/keys/me", headers=as_user(BOB))).json()
        if k["id"] == k1["id"]
    ]
    assert first["block_reason"] == "Key 出现在公开仓库里"
    # 管理员不能重置个人 Key、不能改它的模型
    assert (await client.post(f"/api/v1/keys/{k2['id']}/reset", headers=A)).status_code == 403
    assert (
        await client.put(f"/api/v1/keys/{k2['id']}", headers=A, json={"models": []})
    ).status_code == 403


async def test_internal_client_keys(client: httpx.AsyncClient) -> None:
    a = await _account(client)
    await _model(
        client,
        "m1",
        [{"account_id": a["id"], "upstream_model": "deepseek-v4-pro"}],
        visibility="public",
    )
    await _model(
        client,
        "m2",
        [{"account_id": a["id"], "upstream_model": "deepseek-v4-flash"}],
        visibility="private",
    )
    # Agent：建时默认分配全部公有模型，马上能用
    r = await client.post(
        "/internal/client-keys",
        headers=as_app(),
        json={"client_type": "Agent", "client_code": "agent-1", "name": "编程助手"},
    )
    body = r.json()
    assert body["status"] == "active" and [m["name"] for m in body["models"]] == ["m1"]
    assert body["models"][0]["capabilities"]["tools"] is True
    secret = body["key"]
    # 再取一次是同一把（原文加密存，取得回来）
    r = await client.get(
        "/internal/client-keys",
        headers=as_app(),
        params={"client_type": "Agent", "client_code": "agent-1"},
    )
    assert r.json()["key"] == secret
    # Service：等管理员分配
    r = await client.post(
        "/internal/client-keys",
        headers=as_app(),
        json={"client_type": "Service", "client_code": "session-title"},
    )
    assert r.json()["status"] == "pending_models"
    # 别的应用不能操作；缺租户 401
    assert (
        await client.post(
            "/internal/client-keys",
            headers=as_app("kb"),
            json={"client_type": "Agent", "client_code": "x"},
        )
    ).status_code == 403
    assert (
        await client.get("/internal/models", headers={"X-Caller-App": "atlas"})
    ).status_code == 401
    models = (await client.get("/internal/models", headers=as_app())).json()["models"]
    assert {m["name"] for m in models} == {"m1", "m2"} and models[0]["provider"] == "deepseek"
    # 管理员重置客户端 Key：旧的再保留一会儿
    [key] = [
        k
        for k in (await client.get("/api/v1/keys", headers=A, params={"key_type": "client"})).json()
        if k["client_code"] == "agent-1"
    ]
    await client.post(f"/api/v1/keys/{key['id']}/reset", headers=A)
    r = await client.get(
        "/internal/client-keys",
        headers=as_app(),
        params={"client_type": "Agent", "client_code": "agent-1"},
    )
    assert r.json()["key"] != secret


async def test_tenants_are_isolated(client: httpx.AsyncClient) -> None:
    a = await _account(client)
    await _model(client, "m1", [{"account_id": a["id"]}])
    other = as_user(ADMIN, OTHER_TENANT)
    assert (await client.get("/api/v1/accounts", headers=other)).json() == []
    assert (await client.get("/api/v1/models", headers=other)).json() == []
    # 同名模型在另一个租户可以登记
    r = await client.post(
        "/api/v1/accounts",
        headers=other,
        json={
            "name": "公司账号",
            "provider": "deepseek",
            "endpoints": {"anthropic": "https://x.test/anthropic"},
            "secret": "k",
        },
    )
    assert r.status_code == 201
    r = await client.post(
        "/api/v1/models",
        headers=other,
        json={"name": "m1", "routes": [{"account_id": r.json()["id"]}]},
    )
    assert r.status_code == 201
    # 用另一个租户的账号做线路 → 账号不存在
    r = await client.post(
        "/api/v1/models",
        headers=A,
        json={"name": "m2", "routes": [{"account_id": r.json()["routes"][0]["account_id"]}]},
    )
    assert r.status_code == 400 and "账号不存在" in r.json()["detail"]
    # 缺租户
    assert (await client.get("/api/v1/models", headers={"X-Uid": ADMIN})).status_code == 401


# ── 和网关连起来 ───────────────────────────────────────────────────────


@pytest.fixture
async def gateway(upstream: FakeUpstream) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    from atlas_modelgateway.gateway.app import build_runtime, create_gateway_app
    from atlas_modelgateway.gateway.format import AddressGuard
    from atlas_modelgateway.settings import get_settings

    settings = get_settings()
    runtime = build_runtime(
        settings, transport=upstream.transport(), guard=AddressGuard(enabled=False)
    )
    app = create_gateway_app(settings, runtime=runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        yield c, runtime
    await runtime.upstream.aclose()


async def test_platform_to_gateway_end_to_end(
    client: httpx.AsyncClient, gateway: Any, upstream: FakeUpstream
) -> None:
    gw, runtime = gateway
    a = await _account(client)
    await _model(
        client,
        "deepseek-v4-pro",
        [{"account_id": a["id"]}],
        visibility="public",
        price={"input": "4", "output": "16"},
    )
    key = (
        await client.post(
            "/api/v1/keys/me",
            headers=as_user(BOB),
            json={"label": "Claude Code", "models": ["deepseek-v4-pro"]},
        )
    ).json()
    upstream.on("a.test", anthropic_json("你好", usage_in=1000, usage_out=500))
    r = await gw.post(
        "/v1/messages",
        headers={"authorization": f"Bearer {key['secret']}"},
        json={
            "model": "deepseek-v4-pro",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    sent = upstream.hits("a.test")[-1]
    assert sent.headers["authorization"] == "Bearer sk-upstream-1234"  # 账号密钥解密后发给上游
    await runtime.records.flush()
    page = (await client.get("/api/v1/calls", headers=as_user(BOB))).json()
    [call] = page["data"]
    assert call["model"] == "deepseek-v4-pro" and Decimal(call["cost"]) == Decimal("0.012")
    summary = (
        await client.get("/api/v1/calls/summary", headers=as_user(BOB), params={"by": "key"})
    ).json()
    assert summary[0]["calls"] == 1 and "Claude Code" in summary[0]["label"]
    [mine] = (await client.get("/api/v1/keys/me", headers=as_user(BOB))).json()
    assert Decimal(mine["spent_this_month"]) == Decimal("0.012") and mine["last_used_at"]
    # 普通用户不能看全部
    assert (
        await client.get("/api/v1/calls", headers=as_user(BOB), params={"scope": "all"})
    ).status_code == 403
    # 停用这把 Key：网关拒绝（配置快照清掉缓存模拟版本号变了）
    await client.post(f"/api/v1/keys/me/{key['id']}/block", headers=as_user(BOB))
    runtime.config.forget()
    r = await gw.post(
        "/v1/messages",
        headers={"x-api-key": key["secret"]},
        json={
            "model": "deepseek-v4-pro",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 401
