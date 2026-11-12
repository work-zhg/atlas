"""控制台改版（P1）的后端：默认智能体、回答反馈、知识库「使用者能查阅的全部」。

★ 默认智能体只是平台配置里的一个指针：智能体本身是普通智能体，能不能看到照常按共享判。
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.main import create_app
from atlas_agent.providers import knowledge as kb
from atlas_agent.repositories.thread import ThreadRepository
from atlas_engine.contracts.spec import KnowledgeSetSpec, KnowledgeSpec
from httpx import ASGITransport

from tests.fake_uc import AS_USER


@pytest.fixture
async def client() -> httpx.AsyncClient:
    app = create_app()
    # 运行期配置的 key 在 lifespan 里登记；ASGITransport 不跑 lifespan，这里补上
    await app.state.runtime_config.config.register()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def _agent(client: httpx.AsyncClient, slug: str = "general", *, enable: bool = True) -> str:
    r = await client.post(
        "/v1/agents",
        json={
            "slug": slug,
            "name": "通用助手",
            "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
        },
    )
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    if enable:
        r = await client.post(f"/v1/agents/{agent_id}/status", json={"status": "enabled"})
        assert r.status_code == 200, r.text
    return agent_id


# ───────────────────────────────────────────── 默认智能体


async def test_no_default_agent_is_404(client: httpx.AsyncClient, clean_db: None) -> None:
    await client.put("/v1/agents/default", json={"agent_id": None})
    r = await client.get("/v1/agents/default")
    assert r.status_code == 404


async def test_set_then_get_default_agent(client: httpx.AsyncClient, clean_db: None) -> None:
    agent_id = await _agent(client)
    r = await client.put("/v1/agents/default", json={"agent_id": agent_id})
    assert r.status_code == 200, r.text
    r = await client.get("/v1/agents/default")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == agent_id
    # 取消默认
    assert (await client.put("/v1/agents/default", json={"agent_id": None})).status_code == 200
    assert (await client.get("/v1/agents/default")).status_code == 404


async def test_only_enabled_agent_can_be_default(client: httpx.AsyncClient, clean_db: None) -> None:
    draft = await _agent(client, "draft-one", enable=False)
    r = await client.put("/v1/agents/default", json={"agent_id": draft})
    assert r.status_code == 400, r.text


async def test_archived_default_falls_back_to_404(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """默认智能体后来被归档：首页不该再用它（前端退回让用户自己选）。"""
    agent_id = await _agent(client)
    await client.put("/v1/agents/default", json={"agent_id": agent_id})
    await client.post(f"/v1/agents/{agent_id}/status", json={"status": "archived"})
    assert (await client.get("/v1/agents/default")).status_code == 404
    await client.put("/v1/agents/default", json={"agent_id": None})


# ───────────────────────────────────────────── 回答反馈


async def _thread_with_answer(client: httpx.AsyncClient) -> tuple[str, str, str]:
    agent_id = await _agent(client, "feedback-agent")
    thread = (await client.post("/v1/threads", json={"agent_id": agent_id})).json()["id"]
    async with get_sessionmaker()() as s:
        repo = ThreadRepository(s)
        question = await repo.add_message(
            thread_id=UUID(thread), role="user", content=[{"type": "text", "text": "hi"}]
        )
        answer = await repo.add_message(
            thread_id=UUID(thread), role="assistant", content=[{"type": "text", "text": "hello"}]
        )
        await s.commit()
        return thread, str(question.message_uuid), str(answer.message_uuid)


async def test_feedback_upsert_list_and_remove(client: httpx.AsyncClient, clean_db: None) -> None:
    thread, _, answer = await _thread_with_answer(client)
    url = f"/v1/threads/{thread}/messages/{answer}/feedback"

    r = await client.put(url, json={"rating": "up"})
    assert r.status_code == 200, r.text
    # 再点就是改：同一个人对同一条回答只有一条
    r = await client.put(
        url, json={"rating": "down", "reasons": ["内容不准确"], "comment": "数字错了"}
    )
    assert r.status_code == 200, r.text
    listed = (await client.get(f"/v1/threads/{thread}/feedback")).json()["data"]
    assert listed == [
        {"message_id": answer, "rating": "down", "reasons": ["内容不准确"], "comment": "数字错了"}
    ]

    assert (await client.delete(url)).status_code == 204
    assert (await client.get(f"/v1/threads/{thread}/feedback")).json()["data"] == []


async def test_feedback_only_on_assistant_messages(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    thread, question, _ = await _thread_with_answer(client)
    r = await client.put(
        f"/v1/threads/{thread}/messages/{question}/feedback", json={"rating": "up"}
    )
    assert r.status_code == 404


# ───────────────────────────────────────────── 知识库「使用者能查阅的全部」


def _kb(readable: list[str] | None, seen: list[str]) -> kb.KnowledgeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if readable is None:
            return httpx.Response(503, json={"detail": "down"})
        body = json.loads(request.content)
        assert body == {"dataset_ids": None}  # 不带库 id = 全部能读的
        return httpx.Response(200, json={"data": [{"id": d} for d in readable]})

    return kb.KnowledgeClient("http://kb", transport=httpx.MockTransport(handler))


EVERYONE = KnowledgeSpec(
    sets=(KnowledgeSetSpec(name="我能查阅的资料", scope="all_readable"),)
)


async def test_no_knowledge_sets_asks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(kb, "make_knowledge_client", lambda _s: _kb(["x"], seen))
    got = await kb.resolve_knowledge(object(), KnowledgeSpec())
    assert got.turn is None and got.notices() == [] and seen == []


async def test_all_readable_expands_to_readable_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    """「使用者能读的全部」展开成这个人能读的全部库：不再截成 20 个（知识集方案 §7）。"""
    seen: list[str] = []
    ids = [f"ds-{i:02d}" for i in range(25)]
    monkeypatch.setattr(kb, "make_knowledge_client", lambda _s: _kb(ids, seen))
    got = await kb.resolve_knowledge(object(), EVERYONE)
    assert got.turn is not None and got.notices() == []
    [everyone] = got.turn.sets
    assert [b.id for b in everyone.bases] == ids
    assert seen == ["/internal/v1/datasets/readable"]


async def test_all_readable_with_nothing_readable_disables_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kb, "make_knowledge_client", lambda _s: _kb([], []))
    got = await kb.resolve_knowledge(object(), EVERYONE)
    assert got.turn is None and got.notices() == []


async def test_all_readable_without_knowledge_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """没接知识库 = 没有能读的库：不检索、不报错（默认智能体在没部署知识库的环境也能用）。"""
    monkeypatch.setattr(kb, "make_knowledge_client", lambda _s: None)
    got = await kb.resolve_knowledge(object(), EVERYONE)
    assert got.turn is None and got.notices() == []


async def test_all_readable_when_knowledge_down_tells_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """知识库不可用：这一轮照常进行，但要告诉用户没用知识库（不静默少一种能力）。"""
    monkeypatch.setattr(kb, "make_knowledge_client", lambda _s: _kb(None, []))
    got = await kb.resolve_knowledge(object(), EVERYONE)
    assert got.turn is None
    [(_, data)] = got.notices()
    assert "知识库" in data["message"]


def test_spec_round_trip_keeps_scope() -> None:
    from atlas_agent.schemas.agent import AgentSpecIn

    spec = AgentSpecIn.model_validate(
        {"model": {"model": "claude-opus-5"}, "knowledge": {"scope": "all_readable"}}
    )
    engine = spec.to_engine(slug="g", name="通用助手")
    engine.validate()
    [only] = engine.knowledge.sets
    assert only.scope == "all_readable" and engine.knowledge.enabled


# ───────────────────────────────────────────── P2：列表、草稿 / 发布、调试预览


@pytest.fixture
async def run_client() -> httpx.AsyncClient:
    """带假模型执行器的 app：调试预览要真的建 run。"""
    from tests.test_runs_api import make_app

    app = make_app()
    await app.state.runtime_config.config.register()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def test_list_scope_and_my_fields(client: httpx.AsyncClient, clean_db: None) -> None:
    agent_id = await _agent(client, "list-mine")
    thread = await client.post("/v1/threads", json={"agent_id": agent_id})
    assert thread.status_code == 201, thread.text

    mine = (await client.get("/v1/agents", params={"scope": "mine"})).json()["data"]
    row = next(a for a in mine if a["id"] == agent_id)
    assert row["mine"] is True
    assert row["my_level"] == "OWNER"
    assert row["last_used_at"] is not None  # 我在它上面开过会话
    assert row["has_draft"] is False

    shared = (await client.get("/v1/agents", params={"scope": "shared"})).json()["data"]
    assert all(a["id"] != agent_id for a in shared)


async def test_draft_then_publish(client: httpx.AsyncClient, clean_db: None) -> None:
    agent_id = await _agent(client, "draft-publish")
    detail = (await client.get(f"/v1/agents/{agent_id}")).json()
    spec = {**detail["spec"], "system_prompt": "改过的提示词"}

    r = await client.put(f"/v1/agents/{agent_id}/draft", json={"spec": spec})
    assert r.status_code == 204, r.text
    detail = (await client.get(f"/v1/agents/{agent_id}")).json()
    assert detail["version"] == 1  # 草稿不产生版本
    assert detail["has_draft"] and detail["draft_spec"]["system_prompt"] == "改过的提示词"
    assert detail["spec"]["system_prompt"] == "p"

    r = await client.post(f"/v1/agents/{agent_id}/publish", json={"note": "换了提示词"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2 and not body["has_draft"] and body["draft_spec"] is None
    assert body["spec"]["system_prompt"] == "改过的提示词"
    versions = (await client.get(f"/v1/agents/{agent_id}/versions")).json()["data"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["note"] == "换了提示词"


async def test_publish_draft_agent_enables_without_new_version(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    agent_id = await _agent(client, "draft-agent", enable=False)
    r = await client.post(f"/v1/agents/{agent_id}/publish", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enabled" and r.json()["version"] == 1


async def test_discard_draft(client: httpx.AsyncClient, clean_db: None) -> None:
    agent_id = await _agent(client, "discard-draft")
    spec = (await client.get(f"/v1/agents/{agent_id}")).json()["spec"]
    await client.put(f"/v1/agents/{agent_id}/draft", json={"spec": {**spec, "system_prompt": "x"}})
    assert (await client.delete(f"/v1/agents/{agent_id}/draft")).status_code == 204
    assert (await client.get(f"/v1/agents/{agent_id}")).json()["has_draft"] is False


async def test_preview_thread_runs_on_draft_and_stays_out_of_history(
    run_client: httpx.AsyncClient, clean_db: None
) -> None:
    from atlas_agent.db.models import AgentVersion, Run
    from sqlalchemy import select

    client = run_client
    agent_id = await _agent(client, "preview-agent")
    spec = (await client.get(f"/v1/agents/{agent_id}")).json()["spec"]
    await client.put(
        f"/v1/agents/{agent_id}/draft", json={"spec": {**spec, "system_prompt": "草稿"}}
    )

    r = await client.post(f"/v1/agents/{agent_id}/preview-thread")
    assert r.status_code == 201, r.text
    thread_id = r.json()["id"]
    listed = (await client.get("/v1/threads")).json()["data"]
    assert all(t["id"] != thread_id for t in listed)  # 调试预览不进会话列表

    r = await client.post(
        f"/v1/threads/{thread_id}/runs", json={"content": [{"type": "text", "text": "hi"}]}
    )
    assert r.status_code == 202, r.text
    async with get_sessionmaker()() as s:
        run = (
            await s.execute(select(Run).where(Run.run_uuid == UUID(r.json()["run_id"])))
        ).scalar_one()
        version = (
            await s.execute(
                select(AgentVersion).where(
                    AgentVersion.agent_version_uuid == run.agent_version_uuid
                )
            )
        ).scalar_one()
    assert version.kind == "preview" and version.version < 0
    assert version.spec["system_prompt"] == "草稿"
    # 预览快照不进版本历史
    versions = (await client.get(f"/v1/agents/{agent_id}/versions")).json()["data"]
    assert [v["version"] for v in versions] == [1]


async def test_cli_agent_gets_default_adapter(client: httpx.AsyncClient, clean_db: None) -> None:
    r = await client.post(
        "/v1/agents",
        json={
            "slug": "cli-from-template",
            "name": "编程助手",
            "spec": {
                "kind": "cli",
                "model": {"model": "claude-opus-5"},
                "cli": {"cli_type": "claude-code"},
            },
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["spec"]["cli"]["adapter"] == "node /opt/acp-cli/bin/claude-acp"
