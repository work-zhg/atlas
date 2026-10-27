"""控制面：登记、生命周期、探测、价目、平台用途、授权、额度、迁移（方案设计 §4–§6、§10）。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from fake_llm import Profile
from modelgw_testkit import ADMIN, ALICE, BOB, DIGITAL, anthropic_body, as_user, key_of, register

pytestmark = pytest.mark.asyncio


async def test_register_requires_access_check_and_registers_chat_in_uc(client, llm, perms) -> None:
    llm.models["open"] = Profile(open=True)
    r = await register(client, "open")
    assert r.status_code == 400 and r.json()["code"] == "ACCESS_CHECK_FAILED"
    assert "可以绕过模型网关" in r.json()["detail"]

    llm.models["chat-a"] = Profile()
    llm.models["emb"] = Profile(kind="embedding", protocols=("openai",))
    assert (await register(client, "chat-a")).status_code == 200
    assert (await register(client, "emb", kind="embedding")).status_code == 200
    # 只有对话模型按人授权：嵌入模型不登记数据对象
    assert perms.registered == ["chat-a"]
    assert perms.grants["chat-a"] == {ADMIN: "OWNER"}

    r = await register(client, "chat-a")
    assert r.status_code == 409 and r.json()["code"] == "MODEL_EXISTS"
    r = await client.post("/api/v1/models", json={"name": "Bad Name"}, headers=as_user(ADMIN))
    assert r.status_code == 422


async def test_only_managers_register_and_owners_see_their_models(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    llm.models["chat-b"] = Profile()
    r = await client.post("/api/v1/models", json={"name": "chat-a"}, headers=as_user(ALICE))
    assert r.status_code == 403
    await register(client, "chat-a")
    await register(client, "chat-b")
    perms.grants["chat-b"][ALICE] = "OWNER"
    names = [m["name"] for m in (await client.get("/api/v1/models", headers=as_user(ALICE))).json()]
    assert names == ["chat-b"]
    assert (await client.get("/api/v1/models/chat-a", headers=as_user(ALICE))).status_code == 403
    me = (await client.get("/api/v1/me", headers=as_user(ALICE))).json()
    assert me == {"uid": ALICE, "can_manage": False, "owned_models": ["chat-b"], "owns_all": False}


async def test_activate_needs_limits_and_protocols(client, llm) -> None:
    llm.models["chat-a"] = Profile()
    r = await register(
        client, "chat-a", activate=False, context_window=None, max_output_tokens=None
    )
    assert r.status_code == 201
    r = await client.post("/api/v1/models/chat-a/activate", headers=as_user(ADMIN))
    assert r.status_code == 400 and r.json()["code"] == "LIMITS_REQUIRED"
    await client.patch(
        "/api/v1/models/chat-a",
        json={"context_window": 64000, "max_output_tokens": 4096},
        headers=as_user(ADMIN),
    )
    r = await client.post("/api/v1/models/chat-a/activate", headers=as_user(ADMIN))
    assert r.status_code == 200 and r.json()["status"] == "available"


async def test_update_validates_attributes_and_rechecks_upstream(client, llm) -> None:
    llm.models["chat-a"] = Profile()
    llm.models["open"] = Profile(open=True)
    await register(client, "chat-a")
    r = await client.patch(
        "/api/v1/models/chat-a", json={"capabilities": {"nope": True}}, headers=as_user(ADMIN)
    )
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ATTRIBUTES"
    r = await client.patch(
        "/api/v1/models/chat-a", json={"protocols": ["rerank"]}, headers=as_user(ADMIN)
    )
    assert r.status_code == 400 and r.json()["code"] == "INVALID_PROTOCOL"
    # 换上游 = 换了一条路由：接入检查不过就不让换
    r = await client.patch(
        "/api/v1/models/chat-a", json={"upstream_model": "open"}, headers=as_user(ADMIN)
    )
    assert r.status_code == 400 and r.json()["code"] == "ACCESS_CHECK_FAILED"
    detail = (await client.get("/api/v1/models/chat-a", headers=as_user(ADMIN))).json()
    assert detail["upstream_model"] == "chat-a"


async def test_deprecate_and_retire_due(client, llm, app) -> None:
    from atlas_modelgateway.db.session import get_sessionmaker
    from atlas_modelgateway.db.types import utcnow
    from atlas_modelgateway.registry import Registry
    from atlas_modelgateway.settings import get_settings

    llm.models["chat-a"] = Profile()
    llm.models["chat-b"] = Profile()
    llm.models["emb"] = Profile(kind="embedding", protocols=("openai",))
    await register(client, "chat-a")
    await register(client, "chat-b")
    await register(client, "emb", kind="embedding")
    r = await client.post(
        "/api/v1/models/chat-a/deprecate", json={"replacement": "emb"}, headers=as_user(ADMIN)
    )
    assert r.json()["code"] == "INVALID_REPLACEMENT"
    retire_at = (utcnow() + timedelta(days=30)).isoformat()
    r = await client.post(
        "/api/v1/models/chat-a/deprecate",
        json={"replacement": "chat-b", "retire_at": retire_at},
        headers=as_user(ADMIN),
    )
    assert r.json()["status"] == "deprecated" and r.json()["replacement"] == "chat-b"

    async with get_sessionmaker()() as session:
        row = await Registry(session, get_settings()).get("chat-a")
        row.retire_at = utcnow() - timedelta(minutes=1)
        await session.commit()
    async with get_sessionmaker()() as session:
        assert await Registry(session, get_settings()).retire_due() == ["chat-a"]
        await session.commit()
    detail = (await client.get("/api/v1/models/chat-a", headers=as_user(ADMIN))).json()
    assert detail["status"] == "retired" and detail["retired_at"]


async def test_probe_suggests_capabilities_and_apply_writes_them(client, llm) -> None:
    llm.models["chat-a"] = Profile(
        protocols=("anthropic",),
        supports=frozenset(
            {
                "tools",
                "parallel_tools",
                "thinking_adaptive",
                "thinking_budget",
                "effort",
                "cache",
                "vision",
            }
        ),
    )
    await register(
        client, "chat-a", protocols=["anthropic", "openai"], activate=False, capabilities={}
    )
    r = await client.post("/api/v1/models/chat-a/probes", headers=as_user(ADMIN))
    assert r.status_code == 201, r.text
    probe = r.json()
    checks = {(c["protocol"], c["check"]): c["ok"] for c in probe["results"]}
    assert checks[("anthropic", "basic")] and not checks[("openai", "basic")]
    assert checks[("anthropic", "multi_turn_tools")]
    suggested = probe["suggested"]
    assert suggested["protocols"] == ["anthropic"]  # openai 不通：不标为支持
    caps = suggested["capabilities"]
    assert caps["tools"] and caps["parallel_tools"] and caps["vision"]
    assert caps["thinking"] and caps["adaptive_thinking"] and caps["thinking_always_on"]
    assert caps["effort"] and caps["prompt_cache"] and caps["stream_usage"]
    assert not caps["temperature"]
    assert suggested["input_modalities"] == ["text", "image"]
    assert "protocols" in probe["differences"]

    # 探测不直接改目录：确认之后才写进去
    before = (await client.get("/api/v1/models/chat-a", headers=as_user(ADMIN))).json()
    assert before["protocols"] == ["anthropic", "openai"]
    r = await client.post(
        f"/api/v1/models/chat-a/probes/{probe['id']}/apply", headers=as_user(ADMIN)
    )
    assert r.status_code == 200
    after = r.json()
    assert after["protocols"] == ["anthropic"] and after["capabilities"]["effort"]
    probes = (await client.get("/api/v1/models/chat-a/probes", headers=as_user(ADMIN))).json()
    assert probes[0]["applied_by"] == ADMIN


async def test_probe_embedding_dimensions(client, llm) -> None:
    llm.models["emb"] = Profile(kind="embedding", protocols=("openai",))
    await register(client, "emb", kind="embedding", activate=False)
    probe = (await client.post("/api/v1/models/emb/probes", headers=as_user(ADMIN))).json()
    assert probe["suggested"] == {"protocols": ["openai"], "dimensions": 4}


async def test_prices_are_versioned_and_tiers_apply(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile(
        usage={"input": 300_000, "output": 1000, "cache_read": 0, "cache_write": 0}
    )
    await register(client, "chat-a")
    r = await client.post(
        "/api/v1/models/chat-a/prices",
        json={"input": "1", "output": "2", "tiers": [{"above_input_tokens": 200000, "input": "3"}]},
        headers=as_user(ADMIN),
    )
    assert r.status_code == 201, r.text
    await client.post(
        "/api/v1/models/chat-a/prices", json={"input": "10", "output": "20"}, headers=as_user(ADMIN)
    )
    prices = (await client.get("/api/v1/models/chat-a/prices", headers=as_user(ADMIN))).json()
    assert [p["input"] for p in prices] == ["10.000000", "1.000000"]
    detail = (await client.get("/api/v1/models/chat-a", headers=as_user(ADMIN))).json()
    assert detail["price"]["input"] == "10.000000"

    from atlas_modelgateway.db.models import GwModelPrice
    from atlas_modelgateway.usage import Usage, cost_of

    tiered = GwModelPrice(
        input=Decimal("1"),
        output=Decimal("2"),
        tiers=[{"above_input_tokens": 200000, "input": "3"}],
    )
    assert cost_of(tiered, Usage(input_tokens=300_000, output_tokens=1000)) == Decimal("0.90200000")
    assert cost_of(tiered, Usage(input_tokens=100_000, output_tokens=1000)) == Decimal("0.10200000")
    # 缓存读没有单独定价：按输入价；分段按含缓存的全部输入选
    assert cost_of(tiered, Usage(input_tokens=0, cache_read_tokens=100_000)) == Decimal(
        "0.10000000"
    )
    assert cost_of(tiered, Usage(input_tokens=0, cache_read_tokens=300_000)) == Decimal(
        "0.90000000"
    )


async def test_purposes_validate_kind(client, llm) -> None:
    llm.models["chat-a"] = Profile()
    llm.models["emb"] = Profile(kind="embedding", protocols=("openai",))
    await register(client, "chat-a")
    await register(client, "emb", kind="embedding")
    r = await client.put(
        "/api/v1/purposes/kb_embedding", json={"default_model": "chat-a"}, headers=as_user(ADMIN)
    )
    assert r.status_code == 400 and r.json()["code"] == "INVALID_PURPOSE_MODEL"
    r = await client.put("/api/v1/purposes/nope", json={}, headers=as_user(ADMIN))
    assert r.status_code == 404
    r = await client.put(
        "/api/v1/purposes/title", json={"default_model": "chat-a"}, headers=as_user(ADMIN)
    )
    assert r.json()["allowed_models"] == ["chat-a"]
    catalog = (await client.get("/internal/catalog")).json()
    purposes = {p["code"]: p for p in catalog["purposes"]}
    assert purposes["title"]["default_model"] == "chat-a"
    assert purposes["agent_default"]["callable"] is False
    assert {m["name"] for m in catalog["models"]} == {"chat-a", "emb"}


async def test_grants_go_through_the_owner(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    body = {"subjects": [{"type": "user", "id": DIGITAL}]}
    r = await client.post("/api/v1/models/chat-a/grants", json=body, headers=as_user(BOB))
    assert r.status_code == 403  # 不是所有者
    r = await client.post("/api/v1/models/chat-a/grants", json=body, headers=as_user(ADMIN))
    assert r.status_code == 201
    access = (await client.get(f"/internal/subjects/{DIGITAL}/access")).json()
    assert access["models"] == ["chat-a"]
    [acl] = (await client.get("/api/v1/models/chat-a/grants", headers=as_user(ADMIN))).json()
    await client.delete(f"/api/v1/models/chat-a/grants/{acl['id']}", headers=as_user(ADMIN))
    key = await key_of(client, DIGITAL)
    r = await client.post(
        "/v1/messages",
        json=anthropic_body("chat-a"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    assert r.json()["error"]["reason"] == "forbidden"  # 本进程里收回立即生效


async def test_budget_warns_when_spending_nears_amount(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile(
        usage={"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0}
    )
    await register(client, "chat-a")
    await client.post(
        "/api/v1/models/chat-a/prices", json={"input": "4", "output": "8"}, headers=as_user(ADMIN)
    )
    r = await client.post(
        "/api/v1/budgets",
        json={"name": "DeepSeek 充值", "provider": "fake", "amount": "10"},
        headers=as_user(ADMIN),
    )
    assert r.status_code == 201 and not r.json()["warning"]
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    for _ in range(2):
        await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=headers)
    [budget] = (await client.get("/api/v1/budgets", headers=as_user(ADMIN))).json()
    assert Decimal(budget["spent"]) == Decimal("8") and Decimal(budget["remaining"]) == Decimal("2")
    assert budget["warning"]
    alerts = {
        a["kind"] for a in (await client.get("/api/v1/alerts", headers=as_user(ADMIN))).json()
    }
    assert "budget_low" in alerts


async def test_import_catalog_from_atlas(client, llm, perms) -> None:
    from atlas_modelgateway.db.session import get_sessionmaker
    from atlas_modelgateway.migrate import import_catalog
    from atlas_modelgateway.settings import get_settings

    llm.models["deepseek-chat"] = Profile()
    llm.models["old-model"] = Profile()
    exported = {
        "models": [
            {
                "name": "deepseek-chat",
                "kind": "chat",
                "display_name": "DeepSeek Chat",
                "protocols": ["anthropic"],
                "capabilities": {"tools": True, "stream_usage": True},
                "context_window": 128000,
                "max_output_tokens": 8192,
                "available": True,
            },
            {"name": "old-model", "kind": "chat", "protocols": ["anthropic"], "available": False},
            {"name": "missing", "kind": "chat", "protocols": ["anthropic"], "available": True},
        ],
        "digital_employees": [
            {"uid": DIGITAL, "name": "工单助手", "models": ["deepseek-chat", "missing"]}
        ],
        "purposes": {"title": "deepseek-chat", "agent_default": "deepseek-chat"},
    }
    async with get_sessionmaker()() as session:
        report = await import_catalog(
            session, get_settings(), perms, exported, owner=ADMIN, root="root-dept"
        )
        await session.commit()
    assert report.imported == ["deepseek-chat", "old-model"]
    assert report.activated == ["deepseek-chat"]
    assert [name for name, _ in report.skipped] == ["missing"]  # 上游没有这条路由：接入检查不过
    assert perms.grants["deepseek-chat"]["root-dept"] == "READ"
    assert perms.grants["deepseek-chat"][DIGITAL] == "READ"
    assert sorted(report.purposes) == ["agent_default = deepseek-chat", "title = deepseek-chat"]

    # 再跑一次：登记过的跳过
    async with get_sessionmaker()() as session:
        again = await import_catalog(
            session, get_settings(), perms, exported, owner=ADMIN, root=None
        )
        await session.commit()
    assert again.existing == ["deepseek-chat", "old-model"] and not again.purposes
