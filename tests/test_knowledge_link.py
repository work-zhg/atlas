"""atlas-server 接入知识库服务（doc/atlas-rag-design.html §10）。

spec、注入、工具、MCP 令牌、保存校验。

不连真实的知识库服务：KnowledgeClient 用 httpx.MockTransport。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from atlas_engine.contracts import InvalidSpec
from atlas_server.domain.spec import KnowledgeSpec
from atlas_server.providers.knowledge import (
    KnowledgeClient,
    KnowledgeUnavailable,
    format_context,
    make_knowledge_tool,
    mcp_server_entry,
    sign_mcp_token,
)
from atlas_server.schemas.agent import AgentSpecIn

RECORDS = [
    {
        "dataset_name": "客服",
        "document_name": "faq.txt",
        "content": "退款七天内可申请。",
        "score": 0.9,
    },
    {"dataset_name": "客服", "document_name": "faq.txt", "content": "发票自动开具。", "score": 0.5},
]


def _client(seen: list[dict[str, Any]], status: int = 200) -> KnowledgeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content),
                "token": request.headers.get("x-internal-token"),
            }
        )
        if status != 200:
            return httpx.Response(status, json={"detail": "boom"})
        if request.url.path.endswith("/readable"):
            return httpx.Response(200, json={"data": [{"id": "ds-ok"}]})
        return httpx.Response(200, json={"records": RECORDS})

    return KnowledgeClient("http://kb:8040", "tok", transport=httpx.MockTransport(handler))


def test_spec_snapshot_and_validation() -> None:
    old = AgentSpecIn.model_validate({"model": {"model": "claude-sonnet-5"}})
    assert (
        old.knowledge.dataset_ids == [] and not old.to_engine(slug="a", name="a").knowledge.enabled
    )

    spec = AgentSpecIn.model_validate(
        {
            "model": {"model": "claude-sonnet-5"},
            "knowledge": {"dataset_ids": ["a", "a", "b"], "mode": "tool", "top_k": 3},
        }
    ).to_engine(slug="a", name="a")
    assert spec.knowledge.dataset_ids == ("a", "b")
    spec.validate()

    acp = AgentSpecIn.model_validate(
        {
            "model": {"model": "claude-sonnet-5"},
            "kind": "acp",
            "cli": {"cli_type": "claude", "adapter": "x", "image": "y"},
            "knowledge": {"dataset_ids": ["a"], "mode": "auto"},
        }
    ).to_engine(slug="a", name="a")
    with pytest.raises(InvalidSpec):
        acp.validate()


def test_format_context() -> None:
    out = format_context("怎么退款？", RECORDS)
    assert out.startswith("以下是从知识库检索到的参考资料") and out.endswith("怎么退款？")
    assert "[1] 《faq.txt》" in out and "<knowledge>" in out
    blocks = format_context([{"type": "text", "text": "看图"}], RECORDS)
    assert blocks[0]["type"] == "text" and blocks[1]["text"] == "看图"
    assert format_context("q", []) == "q"


async def test_client_and_tool() -> None:
    seen: list[dict[str, Any]] = []
    k = KnowledgeSpec(dataset_ids=("d1",), mode="tool", top_k=2, score_threshold=0.3)
    tool = make_knowledge_tool(_client(seen), k, names=None, source_ref="thread:x", actor=None)
    # ★ 库 id 不在参数里
    assert set(tool.args) == {"query"}
    text = await tool.ainvoke({"query": "退款"})
    assert "退款七天内可申请" in text
    body = seen[0]["body"]
    assert seen[0]["token"] == "tok" and body["dataset_ids"] == ["d1"]
    assert (
        body["retrieval_config"]["top_k"] == 2
        and body["retrieval_config"]["score_threshold_enabled"]
    )

    broken = make_knowledge_tool(_client([], status=500), k, names=None, source_ref="x", actor=None)
    assert "暂时不可用" in await broken.ainvoke({"query": "退款"})
    with pytest.raises(KnowledgeUnavailable):
        await _client([], status=500).retrieve(k, "q", source="agent", source_ref=None, actor=None)


def test_mcp_token_matches_knowledge_service() -> None:
    from atlas_knowledge.api.mcp import TokenInvalid, verify_token

    thread = uuid4()
    token = sign_mcp_token("s3cret", dataset_ids=["d1", "d2"], thread_id=thread, actor=None)
    claims = verify_token("s3cret", token)
    assert claims["d"] == ["d1", "d2"] and claims["t"] == str(thread)
    with pytest.raises(TokenInvalid):
        verify_token("other", token)

    entry = mcp_server_entry(
        _client([]), KnowledgeSpec(dataset_ids=("d1",), mode="tool"), thread_id=thread, actor=None
    )
    assert (
        entry["url"] == "http://kb:8040/mcp/"
        and entry["headers"][0]["name"] == "X-Atlas-Knowledge-Token"
    )


async def test_agent_save_checks_new_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlas_server.auth.principal import Principal
    from atlas_server.errors import InvalidReference
    from atlas_server.services import agent as agent_mod

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_mod, "make_knowledge_client", lambda _s: _client(seen))
    svc = agent_mod.AgentService(session=None, settings=object())  # type: ignore[arg-type]
    p = Principal(kind="user", owner_id=uuid4(), operations=frozenset({"agent:create"}))

    def spec(*ids: str) -> AgentSpecIn:
        return AgentSpecIn.model_validate(
            {"model": {"model": "claude-sonnet-5"}, "knowledge": {"dataset_ids": list(ids)}}
        )

    await svc._check_knowledge(spec("ds-ok"), previous=None, principal=p)
    assert seen[-1]["body"]["dataset_ids"] == ["ds-ok"]
    with pytest.raises(InvalidReference):
        await svc._check_knowledge(spec("ds-ok", "ds-secret"), previous=None, principal=p)
    # 沿用旧版本已绑定的库不重新判
    seen.clear()
    await svc._check_knowledge(spec("ds-secret"), previous=spec("ds-secret"), principal=p)
    assert seen == []
