"""★ P2 完成标准（文档 §16）：智能体列表页与编辑器可跑真数据。

覆盖 §11.1 的全部路由，重点在两条语义：
  · 保存即产生新版本（§5.4）
  · 保存时就用 engine 的规则校验 spec（§3 D3），而不是等到 run
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_server.main import create_app
from atlas_server.schemas.agent import AgentSpecIn
from httpx import ASGITransport

BUILTIN_SLUG = "general"


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "system_prompt": "你是一名资深数据分析师。",
        "model": {"model": "claude-opus-5"},
        "tool_names": ["write_todos", "filesystem"],
        "subagents": [],
    }
    base.update(overrides)
    return base


def payload(slug: str = "data-analyst", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": slug,
        "name": "数据分析师",
        "description": "连接数仓执行 SQL。",
        "avatar_key": "analyst",
        "spec": spec(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


async def test_create_agent_starts_at_version_1_draft(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    r = await client.post("/v1/agents", json=payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["status"] == "draft"
    assert body["is_builtin"] is False
    assert body["model"] == "claude-opus-5"
    assert body["tool_count"] == 2
    assert body["subagent_count"] == 0


async def test_duplicate_slug_conflicts(client: httpx.AsyncClient, clean_db: None) -> None:
    await client.post("/v1/agents", json=payload())
    r = await client.post("/v1/agents", json=payload(name="另一个"))
    assert r.status_code == 409
    assert r.json()["error"]["kind"] == "slug_taken"


async def test_invalid_slug_rejected(client: httpx.AsyncClient, clean_db: None) -> None:
    r = await client.post("/v1/agents", json=payload(slug="Bad_Slug"))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ★ 保存时就跑 engine 校验 —— 而不是等到某次 run 才 400
# ---------------------------------------------------------------------------


async def test_temperature_on_opus_rejected_at_save_time(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """opus-5 传 temperature 网关会 400；编辑器点保存时就该拦下来（§3 D3）。"""
    bad = spec(model={"model": "claude-opus-5", "temperature": 0.2})
    r = await client.post("/v1/agents", json=payload(spec=bad))
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["kind"] == "invalid_spec"
    assert "temperature" in err["message"]


async def test_effort_on_haiku_rejected_at_save_time(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    bad = spec(model={"model": "claude-haiku-4-5", "effort": "low"})
    r = await client.post("/v1/agents", json=payload(spec=bad))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["kind"] == "invalid_spec"


async def test_haiku_without_effort_is_accepted(client: httpx.AsyncClient, clean_db: None) -> None:
    """auto 解析：haiku 不发 effort/thinking，配置本身是合法的。"""
    ok = spec(model={"model": "claude-haiku-4-5"})
    r = await client.post("/v1/agents", json=payload(slug="summarizer", spec=ok))
    assert r.status_code == 201, r.text


async def test_duplicate_subagent_names_rejected(client: httpx.AsyncClient, clean_db: None) -> None:
    sub = {
        "name": "research",
        "description": "d",
        "system_prompt": "p",
        "model": {"model": "claude-sonnet-5"},
    }
    bad = spec(subagents=[sub, dict(sub)])
    r = await client.post("/v1/agents", json=payload(spec=bad))
    assert r.status_code == 400
    assert r.json()["error"]["kind"] == "invalid_spec"


# ---------------------------------------------------------------------------
# ★ 保存即产生新版本（§5.4）
# ---------------------------------------------------------------------------


async def test_patch_spec_creates_new_version(client: httpx.AsyncClient, clean_db: None) -> None:
    created = (await client.post("/v1/agents", json=payload())).json()
    aid = created["id"]

    updated = spec(system_prompt="改过的提示词")
    r = await client.patch(f"/v1/agents/{aid}", json={"spec": updated})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["spec"]["system_prompt"] == "改过的提示词"

    versions = (await client.get(f"/v1/agents/{aid}/versions")).json()["data"]
    assert [v["version"] for v in versions] == [2, 1]
    # 旧版本内容原样保留 —— 历史 run 靠它解释
    assert versions[1]["spec"]["system_prompt"] == "你是一名资深数据分析师。"


async def test_patch_metadata_only_does_not_bump_version(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    created = (await client.post("/v1/agents", json=payload())).json()
    aid = created["id"]
    r = await client.patch(f"/v1/agents/{aid}", json={"name": "新名字"})
    assert r.status_code == 200
    assert r.json()["name"] == "新名字"
    assert r.json()["version"] == 1  # 未改 spec 就不该产生新版本


async def test_patch_with_invalid_spec_does_not_create_version(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    created = (await client.post("/v1/agents", json=payload())).json()
    aid = created["id"]
    bad = spec(model={"model": "claude-opus-5", "temperature": 0.9})
    assert (await client.patch(f"/v1/agents/{aid}", json={"spec": bad})).status_code == 400
    versions = (await client.get(f"/v1/agents/{aid}/versions")).json()["data"]
    assert len(versions) == 1  # 校验失败不留半截版本


# ---------------------------------------------------------------------------
# 列表 / 过滤
# ---------------------------------------------------------------------------


async def test_list_filters(client: httpx.AsyncClient, clean_db: None) -> None:
    await client.post("/v1/agents", json=payload())
    await client.post(
        "/v1/agents",
        json=payload(
            slug="doc-writer",
            name="文档撰写",
            description="生成中文文档",
            spec=spec(model={"model": "claude-sonnet-5"}),
        ),
    )

    all_ = (await client.get("/v1/agents")).json()["data"]
    slugs = {a["slug"] for a in all_}
    assert {"data-analyst", "doc-writer", BUILTIN_SLUG} <= slugs

    by_model = (await client.get("/v1/agents", params={"model": "claude-opus-5"})).json()
    assert {a["slug"] for a in by_model["data"]} == {"data-analyst"}

    by_q = (await client.get("/v1/agents", params={"q": "文档"})).json()
    assert {a["slug"] for a in by_q["data"]} == {"doc-writer"}

    by_status = (await client.get("/v1/agents", params={"status": "enabled"})).json()
    assert {a["slug"] for a in by_status["data"]} == {BUILTIN_SLUG}


# ---------------------------------------------------------------------------
# 状态 / 删除
# ---------------------------------------------------------------------------


async def test_status_transition(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = (await client.post("/v1/agents", json=payload())).json()["id"]
    r = await client.post(f"/v1/agents/{aid}/status", json={"status": "enabled"})
    assert r.status_code == 200
    assert r.json()["status"] == "enabled"


async def test_builtin_agent_cannot_be_deleted(client: httpx.AsyncClient, clean_db: None) -> None:
    listed = (await client.get("/v1/agents", params={"q": BUILTIN_SLUG})).json()["data"]
    builtin = next(a for a in listed if a["slug"] == BUILTIN_SLUG)
    r = await client.delete(f"/v1/agents/{builtin['id']}")
    assert r.status_code == 409
    assert r.json()["error"]["kind"] == "builtin_agent_protected"


async def test_delete_archives_agent_and_its_threads(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """★ DELETE 是归档而非物理删除。

    物理删除会级联毁掉 agent_version，而 run.agent_version_id 正靠它解释历史（§5.4）；
    且 thread.agent_id 是 NOT NULL，有会话时物理删除直接违反外键。
    """
    aid = (await client.post("/v1/agents", json=payload())).json()["id"]
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]

    r = await client.delete(f"/v1/agents/{aid}")
    assert r.status_code == 200
    assert r.json()["archived_threads"] == 1

    # agent 仍可读取（历史可解释），但已归档
    assert (await client.get(f"/v1/agents/{aid}")).json()["status"] == "archived"
    assert (await client.get(f"/v1/threads/{tid}")).json()["status"] == "archived"


async def test_get_missing_agent_404(client: httpx.AsyncClient, clean_db: None) -> None:
    r = await client.get("/v1/agents/00000000-0000-0000-0000-0000000000ff")
    assert r.status_code == 404
    assert r.json()["error"]["kind"] == "not_found"


# ---------------------------------------------------------------------------
# 内置 agent 的 spec 必须能被 schema 解回来（迁移与 schema 不能漂移）
# ---------------------------------------------------------------------------


async def test_builtin_agent_spec_is_loadable(client: httpx.AsyncClient, clean_db: None) -> None:
    listed = (await client.get("/v1/agents", params={"q": BUILTIN_SLUG})).json()["data"]
    builtin = next(a for a in listed if a["slug"] == BUILTIN_SLUG)
    detail = (await client.get(f"/v1/agents/{builtin['id']}")).json()

    parsed = AgentSpecIn.model_validate(detail["spec"])
    parsed.to_engine(slug=BUILTIN_SLUG, name=detail["name"]).validate()
    assert parsed.model.model == "claude-sonnet-5"
