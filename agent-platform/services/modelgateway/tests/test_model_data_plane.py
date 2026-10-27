"""数据面：认人、判权、模型状态、转发、计量（模型管理方案设计 §6.4、§7.2、§9.2、§10）。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from fake_llm import Profile
from modelgw_testkit import (
    ADMIN,
    ALICE,
    BOB,
    DEPT,
    DIGITAL,
    anthropic_body,
    as_user,
    key_of,
    openai_body,
    register,
)
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def _calls() -> list[Any]:
    from atlas_modelgateway.db.models import GwModelCall
    from atlas_modelgateway.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(GwModelCall).order_by(GwModelCall.id)))


def _anthropic(key: str | None, **headers: str) -> dict[str, str]:
    out = {"anthropic-version": "2023-06-01", **headers}
    if key is not None:
        out["x-api-key"] = key
    return out


async def test_subject_with_grant_can_call_and_usage_is_recorded(client, llm, perms) -> None:
    llm.models["up-chat"] = Profile()
    assert (await register(client, "chat-a", upstream="up-chat")).status_code == 200
    await client.post(
        "/api/v1/models/chat-a/prices",
        json={"input": "2", "output": "8", "cache_read": "0.5"},
        headers=as_user(ADMIN),
    )
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)

    r = await client.post(
        "/v1/messages",
        json=anthropic_body("chat-a"),
        headers=_anthropic(key, **{"X-Uid": ALICE, "X-Run-Id": "run-1", "X-Agent-Id": "agent-1"}),
    )
    assert r.status_code == 200, r.text
    assert r.json()["content"][0]["text"] == "好的"

    # 转发：模型名换成上游模型名、主体 Key 和身份请求头不到上游、带网关凭据
    path, headers, body = llm.seen[-1]
    assert path == "/v1/messages" and body["model"] == "up-chat"
    assert headers["authorization"] == "Bearer modelgw-test-credential"
    assert "x-api-key" not in headers and "x-uid" not in headers and "x-run-id" not in headers
    assert headers["anthropic-version"] == "2023-06-01"

    [row] = await _calls()
    assert row.outcome == "ok" and row.forwarded and row.model == "chat-a"
    assert str(row.subject_uid) == ALICE and row.dept_id == DEPT
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (100, 20, 0)
    assert row.run_id == "run-1" and row.agent_id == "agent-1" and row.user_uid == ALICE
    # 100 × 2 + 20 × 8 = 360 / 1e6
    assert row.cost == Decimal("0.00036000") and row.currency == "CNY"


async def test_streaming_usage_is_metered_without_buffering(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile(supports=frozenset({"cache"}))
    await register(client, "chat-a")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    r = await client.post(
        "/v1/messages",
        json=anthropic_body("chat-a", stream=True, system=[{"type": "text", "text": "x"}]),
        headers=_anthropic(key),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "message_stop" in r.text
    [row] = await _calls()
    assert row.stream and row.usage_reported
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (100, 20, 30)
    assert row.ttft_ms is not None


async def test_openai_stream_gets_include_usage(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile(supports=frozenset({"cache"}))
    await register(client, "chat-a")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    r = await client.post(
        "/v1/chat/completions",
        json=openai_body("chat-a", stream=True),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200 and "[DONE]" in r.text
    _, _, body = llm.seen[-1]
    assert body["stream_options"] == {"include_usage": True}
    [row] = await _calls()
    # prompt_tokens 含缓存：100 + 30，缓存读 30 → 不含缓存的输入 100
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (100, 30, 20)


async def test_body_is_forwarded_verbatim_when_nothing_changes(client, llm, perms) -> None:
    """模型名不用换、不是 OpenAI 流式：请求体原样转发（提示词缓存不受影响）。"""
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    raw = json.dumps(anthropic_body("chat-a"), ensure_ascii=False, separators=(",", ":"))
    await client.post(
        "/v1/messages",
        content=raw.encode(),
        headers={**_anthropic(key), "content-type": "application/json"},
    )
    assert llm.seen[-1][2] == json.loads(raw)


async def test_rejections_explain_reason_in_protocol_format(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    key = await key_of(client, BOB)
    llm.seen.clear()  # 登记时的接入检查

    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(None))
    assert r.status_code == 401
    assert r.json()["type"] == "error" and r.json()["error"]["reason"] == "key_missing"

    r = await client.post(
        "/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic("gk-wrong")
    )
    assert r.status_code == 401 and r.json()["error"]["reason"] == "key_invalid"

    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["reason"] == "forbidden" and err["forwarded"] is False
    assert err["type"] == "permission_error"

    r = await client.post(
        "/v1/chat/completions", json=openai_body("nope"), headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    assert not llm.seen  # 一个都没转发
    outcomes = [c.outcome for c in await _calls()]
    assert outcomes == ["key_missing", "key_invalid", "forbidden", "model_not_found"]


async def test_model_state_gates(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    llm.models["chat-b"] = Profile()
    await register(client, "chat-a")
    await register(client, "chat-b")
    perms.allow("chat-a", ALICE)
    perms.allow("chat-b", ALICE)
    key = await key_of(client, ALICE)
    h = _anthropic(key)

    await client.post(
        "/api/v1/models/chat-a/disable", json={"reason": "供应商事故"}, headers=as_user(ADMIN)
    )
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=h)
    assert r.status_code == 403 and r.json()["error"]["reason"] == "model_disabled"
    assert "供应商事故" in r.json()["error"]["message"]

    await client.post("/api/v1/models/chat-a/enable", headers=as_user(ADMIN))
    assert (
        await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=h)
    ).status_code == 200

    # 弃用照常可用；下线拒绝并提示替代模型
    r = await client.post(
        "/api/v1/models/chat-a/deprecate", json={"replacement": "chat-b"}, headers=as_user(ADMIN)
    )
    assert r.json()["status"] == "deprecated"
    assert (
        await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=h)
    ).status_code == 200
    await client.post("/api/v1/models/chat-a/retire", headers=as_user(ADMIN))
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=h)
    assert r.status_code == 403 and r.json()["error"]["reason"] == "model_retired"
    assert "chat-b" in r.json()["error"]["message"]


async def test_onboarding_and_protocol_checks(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile(protocols=("openai",))
    await register(client, "chat-a", protocols=["openai"], activate=False)
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    r = await client.post(
        "/v1/chat/completions",
        json=openai_body("chat-a"),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_onboarding"
    await client.post("/api/v1/models/chat-a/activate", headers=as_user(ADMIN))
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    assert r.status_code == 400 and r.json()["error"]["reason"] == "protocol_unsupported"


async def test_platform_purpose_path(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    llm.models["chat-b"] = Profile()
    llm.models["emb"] = Profile(kind="embedding", protocols=("openai",))
    await register(client, "chat-a")
    await register(client, "chat-b")
    await register(client, "emb", kind="embedding")
    r = await client.put(
        "/api/v1/purposes/title", json={"default_model": "chat-a"}, headers=as_user(ADMIN)
    )
    assert r.status_code == 200, r.text
    await client.put(
        "/api/v1/purposes/kb_embedding", json={"allowed_models": ["emb"]}, headers=as_user(ADMIN)
    )

    purpose = {"X-Model-Purpose": "title", "X-Caller-App": "atlas", "X-Uid": ALICE}
    r = await client.post(
        "/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(None, **purpose)
    )
    assert r.status_code == 200, r.text
    # 不在用途的允许范围里
    r = await client.post(
        "/v1/messages", json=anthropic_body("chat-b"), headers=_anthropic(None, **purpose)
    )
    assert r.status_code == 403 and r.json()["error"]["reason"] == "purpose_model_not_allowed"
    # 没说是哪个应用
    r = await client.post(
        "/v1/messages",
        json=anthropic_body("chat-a"),
        headers=_anthropic(None, **{"X-Model-Purpose": "title"}),
    )
    assert r.status_code == 401 and r.json()["error"]["reason"] == "caller_missing"
    # 不是调用的用途
    r = await client.post(
        "/v1/messages",
        json=anthropic_body("chat-a"),
        headers=_anthropic(None, **{"X-Model-Purpose": "agent_default", "X-Caller-App": "atlas"}),
    )
    assert r.json()["error"]["reason"] == "purpose_unknown"

    # 嵌入：只供平台功能
    emb = {"model": "emb", "input": ["你好"]}
    r = await client.post(
        "/v1/embeddings",
        json=emb,
        headers={"X-Model-Purpose": "kb_embedding", "X-Caller-App": "atlas"},
    )
    assert r.status_code == 200 and len(r.json()["data"][0]["embedding"]) == 4
    key = await key_of(client, ADMIN)
    r = await client.post("/v1/embeddings", json=emb, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "platform_only"

    rows = [c for c in await _calls() if c.outcome == "ok"]
    assert [(c.purpose, c.caller_app, c.subject_uid) for c in rows] == [
        ("title", "atlas", None),
        ("kb_embedding", "atlas", None),
    ]
    assert rows[1].input_tokens == 7


async def test_key_reset_disable_and_enable(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    [row] = (await client.get("/api/v1/keys", headers=as_user(ADMIN))).json()
    assert row["issued_by"] == "system" and key not in json.dumps(row)

    await client.post(f"/api/v1/keys/{row['id']}/reset", headers=as_user(ADMIN))
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    assert r.json()["error"]["reason"] == "key_invalid"
    new = await key_of(client, ALICE)
    assert new != key

    await client.post(f"/api/v1/keys/{row['id']}/disable", headers=as_user(ADMIN))
    access = (await client.get(f"/internal/subjects/{ALICE}/access")).json()
    assert access["key"] is None and access["key_status"] == "disabled"  # 停用后不会再自动生成
    await client.post(f"/api/v1/keys/{row['id']}/enable", headers=as_user(ADMIN))
    assert (await key_of(client, ALICE)) not in (key, new)


async def test_subject_access_and_auto_issue(client, llm, perms) -> None:
    from modelgw_testkit import GONE

    llm.models["chat-a"] = Profile()
    llm.models["chat-b"] = Profile()
    await register(client, "chat-a")
    await register(client, "chat-b", activate=False)
    perms.allow("chat-a", DIGITAL)
    perms.allow("chat-b", DIGITAL)
    access = (await client.get(f"/internal/subjects/{DIGITAL}/access")).json()
    # 接入中的不算能用
    assert access["models"] == ["chat-a"] and access["key_status"] == "active"
    gone = (await client.get(f"/internal/subjects/{GONE}/access")).json()
    assert gone["key"] is None and gone["key_status"] == "none"


async def test_authz_uses_stale_result_when_user_center_is_down(
    client, llm, perms, monkeypatch
) -> None:
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    assert (
        await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    ).status_code == 200
    subjects = client._transport.app.state.subjects  # type: ignore[attr-defined]
    monkeypatch.setattr(subjects, "_ttl", 0.0)  # 缓存「已过期」
    perms.down = True
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    assert r.status_code == 200  # 沿用上一次的结果
    monkeypatch.setattr(subjects, "_stale", 0.0)
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    assert r.status_code == 503 and r.json()["error"]["reason"] == "authz_unavailable"


async def test_upstream_failures(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    llm.models["chat-a"].fail_with = 402
    r = await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    assert r.status_code == 402  # 供应商的错误原样交回
    llm.models["chat-a"] = Profile(open=False)
    llm.credential, saved = "rotated", llm.credential
    try:
        r = await client.post(
            "/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key)
        )
    finally:
        llm.credential = saved
    assert r.status_code == 502 and r.json()["error"]["reason"] == "upstream_rejected"
    assert r.json()["error"]["forwarded"] is True
    outcomes = [c.outcome for c in await _calls()]
    assert outcomes == ["upstream_error", "upstream_rejected"]
    alerts = {
        a["kind"] for a in (await client.get("/api/v1/alerts", headers=as_user(ADMIN))).json()
    }
    assert {"upstream_rejected", "provider_402", "unpriced"} <= alerts


async def test_models_list_is_filtered(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    llm.models["chat-b"] = Profile()
    await register(client, "chat-a")
    await register(client, "chat-b")
    perms.allow("chat-a", ALICE)
    key = await key_of(client, ALICE)
    r = await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert [m["id"] for m in r.json()["data"]] == ["chat-a"]


async def test_usage_report_groups(client, llm, perms) -> None:
    llm.models["chat-a"] = Profile()
    await register(client, "chat-a")
    await client.post(
        "/api/v1/models/chat-a/prices", json={"input": "1", "output": "2"}, headers=as_user(ADMIN)
    )
    perms.allow("chat-a", ALICE, BOB)
    for uid in (ALICE, ALICE, BOB):
        key = await key_of(client, uid)
        await client.post("/v1/messages", json=anthropic_body("chat-a"), headers=_anthropic(key))
    rows = (await client.get("/api/v1/usage?group_by=subject", headers=as_user(ADMIN))).json()
    by = {r["key"]: r for r in rows}
    assert by[ALICE]["calls"] == 2 and by[BOB]["calls"] == 1
    assert by[ALICE]["input_tokens"] == 200
    assert Decimal(by[ALICE]["cost"]) == Decimal("0.00028")  # 2 × (100×1 + 20×2) / 1e6
    mine = (await client.get("/api/v1/me/usage", headers=as_user(BOB))).json()
    assert [r["calls"] for r in mine] == [1]
    # 不是管理员：要带上自己是所有者的模型
    assert (await client.get("/api/v1/usage", headers=as_user(BOB))).status_code == 403
