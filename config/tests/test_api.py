"""HTTP 面：认证、上传、运行时视角的可见性、MCP 注册表。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from atlas_config.api.app import create_app
from atlas_config.settings import get_settings
from config_testkit import make_zip, skill_files
from pydantic import SecretStr


@pytest.fixture
async def client(storage) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(storage=storage)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cfg") as c:
        yield c


def _upload(client: httpx.AsyncClient, files: dict[str, bytes], *, user: str = "alice", **form):  # type: ignore[no-untyped-def]
    return client.post(
        "/config/skills",
        files={"file": ("skill.zip", make_zip(files), "application/zip")},
        data={"source": "builtin", **form},
        headers={"X-User-Id": user},
    )


# ───────────────────────────────────────────── 技能


async def test_upload_publish_and_runtime_view(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, skill_files())
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "published"

    version = (await client.get("/internal/skills/demo/versions/1")).json()
    assert version["description"].startswith("演示技能")
    assert version["files"][0]["path"] == "SKILL.md"
    catalog = (await client.get("/internal/skills/catalog")).json()
    assert [(c["slug"], c["latest"]) for c in catalog] == [("demo", 1)]

    text = await client.get("/config/skills/demo/versions/1/files/SKILL.md")
    assert text.status_code == 200 and text.text.startswith("---")


async def test_blocked_upload_returns_hits(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, {"README.md": b"no skill"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["layer"] == "structure"
    assert any("SKILL.md" in h for h in body["hits"])


async def test_review_flow_over_http(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, skill_files(extra={"scripts/a.sh": b"#!/bin/sh\necho hi\n"}))
    upload = resp.json()
    assert upload["status"] == "pending_review"
    # 运行时看不到待审查的上传
    assert (await client.get("/internal/skills/demo/versions/1")).status_code == 404
    assert (await client.get("/internal/skills/catalog")).json() == []
    # 审查人能看包内文件
    script = await client.get(f"/config/skill-uploads/{upload['id']}/files/scripts/a.sh")
    assert script.text.startswith("#!/bin/sh")

    selfie = await client.post(
        f"/config/skill-uploads/{upload['id']}/approve",
        json={"note": "ok"},
        headers={"X-User-Id": "alice"},
    )
    assert selfie.status_code == 403
    ok = await client.post(
        f"/config/skill-uploads/{upload['id']}/approve",
        json={"note": "脚本只 echo"},
        headers={"X-User-Id": "bob"},
    )
    assert ok.status_code == 200 and ok.json()["version"] == 1


async def test_status_actions(client: httpx.AsyncClient) -> None:
    await _upload(client, skill_files())
    bad = await client.post("/config/skills/demo/versions/1/revoke", json={})
    assert bad.status_code == 400
    resp = await client.post("/config/skills/demo/versions/1/revoke", json={"reason": "泄露"})
    assert resp.json()["status"] == "revoked"
    assert (await client.get("/internal/skills/revoked")).json()[0]["reason"] == "泄露"
    again = await client.post("/config/skills/demo/versions/1/enable")
    assert again.status_code == 409


async def test_internal_token(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "internal_token", SecretStr("s3cret"))
    assert (await client.get("/internal/skills/catalog")).status_code == 401
    # ★ 用户身份不能打开内部接口
    assert (
        await client.get("/internal/skills/catalog", headers={"X-User-Id": "admin"})
    ).status_code == 401
    ok = await client.get("/internal/skills/catalog", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


async def test_audit_records_writes(client: httpx.AsyncClient) -> None:
    await _upload(client, skill_files())
    actions = [a["action"] for a in (await client.get("/config/audit?target_id=demo")).json()]
    assert set(actions) == {"skill.upload", "skill.publish"}


# ───────────────────────────────────────────── MCP


_SERPAPI = {
    "name": "serpapi",
    "url": "https://mcp.serpapi.com/mcp",
    "headers": {"Authorization": "Bearer ${SERPAPI_KEY}"},
    "call_timeout_s": 90,
}


async def test_mcp_server_crud(client: httpx.AsyncClient) -> None:
    created = await client.post("/config/mcp/servers", json=_SERPAPI)
    assert created.status_code == 201, created.text
    assert created.json()["review_required"] is True
    assert (await client.post("/config/mcp/servers", json=_SERPAPI)).status_code == 409

    patched = await client.patch("/config/mcp/servers/serpapi", json={"status": "disabled"})
    assert patched.json()["enabled"] is False
    # 运行时只看得见 enabled 的
    assert (await client.get("/internal/mcp/servers")).json() == []
    assert len((await client.get("/config/mcp/servers")).json()) == 1


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "Bearer sk-real-token"}, {"X-Api-Key": "abc123"}, {"Cookie": "sid=1"}],
)
async def test_plaintext_credentials_are_refused(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    resp = await client.post("/config/mcp/servers", json={**_SERPAPI, "headers": headers})
    assert resp.status_code == 422
    assert "占位符" in resp.text


async def test_non_secret_headers_allowed(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/config/mcp/servers", json={**_SERPAPI, "headers": {"X-Region": "cn"}}
    )
    assert resp.status_code == 201


async def test_name_rules(client: httpx.AsyncClient) -> None:
    resp = await client.post("/config/mcp/servers", json={**_SERPAPI, "name": "bad_name"})
    assert resp.status_code == 422  # 下划线会破坏 server__tool 的分隔


async def test_tool_review_upsert(client: httpx.AsyncClient) -> None:
    await client.post("/config/mcp/servers", json=_SERPAPI)
    body = {"tool_name": "search", "digest": "c802aabbccdd", "decision": "approved"}
    assert (await client.post("/config/mcp/servers/serpapi/reviews", json=body)).status_code == 201
    body["decision"] = "rejected"
    await client.post("/config/mcp/servers/serpapi/reviews", json=body)
    reviews = (await client.get("/internal/mcp/reviews?server=serpapi")).json()
    assert [(r["tool_name"], r["decision"]) for r in reviews] == [("search", "rejected")]
    missing = await client.post("/config/mcp/servers/nope/reviews", json=body)
    assert missing.status_code == 404


async def test_import_env() -> None:
    from atlas_config.db.session import get_sessionmaker
    from atlas_config.mcp.service import McpRegistry

    raw = (
        '[{"name":"weather","url":"https://w/mcp","headers":{"Authorization":"Bearer ${W}"}},'
        '{"name":"calc","url":"https://c/mcp","enabled":false}]'
    )
    async with get_sessionmaker()() as session:
        registry = McpRegistry(session)
        assert await registry.import_env(raw, actor="ops") == ["weather", "calc"]
        assert await registry.import_env(raw, actor="ops") == []
        servers = {s.name: s for s in await registry.list()}
        await session.commit()
    assert servers["calc"].enabled is False
    # 导入的默认不要求复核：否则现有 agent 的 MCP 工具一上来就全被关掉
    assert servers["weather"].review_required is False
