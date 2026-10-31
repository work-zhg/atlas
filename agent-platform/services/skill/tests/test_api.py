"""HTTP 面：认证、上传、运行时视角的可见性。（MCP 注册表已迁到 MCP 网关。）"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from atlas_skill.api.app import create_app
from skill_testkit import make_zip, skill_files


@pytest.fixture
async def client(storage) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(storage=storage)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://skill", headers={"X-Uid": "tester"}
    ) as c:
        yield c


def _upload(client: httpx.AsyncClient, files: dict[str, bytes], *, user: str = "alice", **form):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/skills",
        files={"file": ("skill.zip", make_zip(files), "application/zip")},
        data={"source": "builtin", **form},
        headers={"X-Uid": user},
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

    text = await client.get("/api/v1/skills/demo/versions/1/files/SKILL.md")
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
    script = await client.get(f"/api/v1/skill-uploads/{upload['id']}/files/scripts/a.sh")
    assert script.text.startswith("#!/bin/sh")

    selfie = await client.post(
        f"/api/v1/skill-uploads/{upload['id']}/approve",
        json={"note": "ok"},
        headers={"X-Uid": "alice"},
    )
    assert selfie.status_code == 403
    ok = await client.post(
        f"/api/v1/skill-uploads/{upload['id']}/approve",
        json={"note": "脚本只 echo"},
        headers={"X-Uid": "bob"},
    )
    assert ok.status_code == 200 and ok.json()["version"] == 1


async def test_status_actions(client: httpx.AsyncClient) -> None:
    await _upload(client, skill_files())
    bad = await client.post("/api/v1/skills/demo/versions/1/revoke", json={})
    assert bad.status_code == 400
    resp = await client.post("/api/v1/skills/demo/versions/1/revoke", json={"reason": "泄露"})
    assert resp.json()["status"] == "revoked"
    assert (await client.get("/internal/skills/revoked")).json()[0]["reason"] == "泄露"
    again = await client.post("/api/v1/skills/demo/versions/1/enable")
    assert again.status_code == 409


async def test_internal_needs_no_token(client: httpx.AsyncClient) -> None:
    """内网免令牌（内网身份传递 §3）：/internal 只在内网可达（网关 404），不再校验服务间令牌。"""
    ok = await client.get("/internal/skills/catalog", headers={"X-Caller-App": "atlas"})
    assert ok.status_code == 200


async def test_audit_records_writes(client: httpx.AsyncClient) -> None:
    await _upload(client, skill_files())
    actions = [a["action"] for a in (await client.get("/api/v1/audit?target_id=demo")).json()]
    assert set(actions) == {"skill.upload", "skill.publish"}
