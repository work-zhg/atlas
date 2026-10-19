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
from atlas_agent.domain.spec import KnowledgeSpec
from atlas_agent.outbound import CallIdentity, use_identity
from atlas_agent.providers.knowledge import (
    KnowledgeClient,
    KnowledgeUnavailable,
    format_context,
    make_knowledge_tool,
    mcp_server_entry,
    sign_mcp_token,
)
from atlas_agent.schemas.agent import AgentSpecIn
from atlas_engine.contracts import InvalidSpec

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
                "headers": dict(request.headers),
            }
        )
        if status != 200:
            return httpx.Response(status, json={"detail": "boom"})
        if request.url.path.endswith("/readable"):
            return httpx.Response(200, json={"data": [{"id": "ds-ok"}]})
        return httpx.Response(200, json={"records": RECORDS})

    return KnowledgeClient("http://kb:8040", "s3cret", transport=httpx.MockTransport(handler))


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
    tool = make_knowledge_tool(_client(seen), k, names=None, source_ref="thread:x")
    # ★ 库 id 不在参数里
    assert set(tool.args) == {"query"}
    # 内网免令牌：带这一轮 run 的身份请求头（按谁判 = auth-uid，内网身份传递 §8）
    uid, auth_uid = uuid4(), uuid4()
    use_identity(CallIdentity(uid=uid, auth_uid=auth_uid))
    try:
        text = await tool.ainvoke({"query": "退款"})
    finally:
        use_identity(None)
    assert "退款七天内可申请" in text
    body, headers = seen[0]["body"], seen[0]["headers"]
    assert "x-internal-token" not in headers and "actor" not in body
    assert headers["x-uid"] == str(uid) and headers["x-auth-uid"] == str(auth_uid)
    assert headers["x-caller-app"] == "atlas" and body["dataset_ids"] == ["d1"]
    assert (
        body["retrieval_config"]["top_k"] == 2
        and body["retrieval_config"]["score_threshold_enabled"]
    )

    broken = make_knowledge_tool(_client([], status=500), k, names=None, source_ref="x")
    assert "暂时不可用" in await broken.ainvoke({"query": "退款"})
    with pytest.raises(KnowledgeUnavailable):
        await _client([], status=500).retrieve(k, "q", source="agent", source_ref=None)


def test_mcp_token_matches_knowledge_service() -> None:
    from atlas_knowledge.api.mcp import TokenInvalid, verify_token

    thread = uuid4()
    token = sign_mcp_token("s3cret", dataset_ids=["d1", "d2"], thread_id=thread, auth_uid=None)
    claims = verify_token("s3cret", token)
    assert claims["d"] == ["d1", "d2"] and claims["t"] == str(thread)
    with pytest.raises(TokenInvalid):
        verify_token("other", token)

    # u = 按谁判（auth-uid），o = 发起人（只进检索记录）—— 取自这一轮 run 的出站身份
    uid, auth_uid = uuid4(), uuid4()
    use_identity(CallIdentity(uid=uid, auth_uid=auth_uid))
    try:
        entry = mcp_server_entry(
            _client([]), KnowledgeSpec(dataset_ids=("d1",), mode="tool"), thread_id=thread
        )
    finally:
        use_identity(None)
    assert (
        entry["url"] == "http://kb:8040/mcp/"
        and entry["headers"][0]["name"] == "X-Atlas-Knowledge-Token"
    )
    claims = verify_token("s3cret", entry["headers"][0]["value"])
    assert claims["u"] == str(auth_uid) and claims["o"] == str(uid)


async def test_agent_save_checks_new_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlas_agent.auth.principal import Principal
    from atlas_agent.errors import InvalidReference
    from atlas_agent.services import agent as agent_mod

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_mod, "make_knowledge_client", lambda _s: _client(seen))
    svc = agent_mod.AgentService(session=None, settings=object())  # type: ignore[arg-type]
    p = Principal(uid=uuid4(), operations=frozenset({"agent:create"}))

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


def test_context_shows_title_path() -> None:
    rec = {**RECORDS[0], "title_path": ["员工手册", "差旅", "住宿标准"]}
    out = format_context("住宿能报多少", [rec])
    assert "[1] 《faq.txt》 员工手册 > 差旅 > 住宿标准（知识库：客服）" in out


async def test_client_propagates_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """检索请求带 traceparent：知识库那边的 span 挂在 atlas.knowledge.retrieve 下。"""
    import atlas_agent.providers.knowledge as mod
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(mod, "tracer", lambda: provider.get_tracer("test"))

    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("traceparent"))
        return httpx.Response(200, json={"records": RECORDS, "retrieval_id": "r-1"})

    client = KnowledgeClient("http://kb", transport=httpx.MockTransport(handler))
    k = KnowledgeSpec(dataset_ids=("d1",), mode="auto")
    with provider.get_tracer("test").start_as_current_span("run") as run:
        await client.retrieve(k, "退款", source="agent", source_ref=None)

    span = next(s for s in exporter.get_finished_spans() if s.name == "atlas.knowledge.retrieve")
    assert span.parent is not None and span.parent.span_id == run.get_span_context().span_id
    assert span.attributes is not None and span.attributes["atlas.knowledge.retrieval_id"] == "r-1"
    trace_id = format(span.context.trace_id, "032x")
    span_id = format(span.context.span_id, "016x")
    assert len(headers) == 1 and (headers[0] or "").startswith(f"00-{trace_id}-{span_id}-")
