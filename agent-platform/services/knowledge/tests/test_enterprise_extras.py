"""企业级设计的补充项：按规则选解析器、解析异常转待审核、向量化用量、自适应多查询、
分段级元数据覆盖。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from atlas_knowledge.errors import Invalid
from atlas_knowledge.rag import index_processor as ip
from atlas_knowledge.rag.embedding import CacheEmbedding
from atlas_knowledge.rag.parsers import parse
from conftest import FakeEmbedding, run_indexing
from pdf_samples import employee_handbook, partly_scanned

ALICE = {"X-Uid": "alice"}
CUSTOM = {
    "mode": "custom",
    "rules": {"segmentation": {"separator": "\\n\\n", "max_tokens": 200, "chunk_overlap": 0}},
}


async def _dataset(client: Any, **body: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/datasets",
        json={"name": "制度库", "embedding_model": "fake-embedding", **body},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _upload(
    client: Any, rt: Any, ds_id: str, data: bytes, name: str, rule: dict[str, Any] = CUSTOM
) -> dict[str, Any]:
    r = await client.post(
        f"/v1/datasets/{ds_id}/documents",
        files=[("files", (name, data, "application/octet-stream"))],
        data={"process_rule": json.dumps(rule)},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    await run_indexing(rt)
    return r.json()["documents"][0]


# ───────────────────────────────────────────── 按处理规则选解析器


def test_parser_option() -> None:
    _, rules = ip.normalize_process_rule(
        "custom", {**CUSTOM["rules"], "parser": "text"}, doc_form="text_model", max_length=4000
    )
    assert rules["parser"] == "text"
    _, rules = ip.normalize_process_rule("automatic", None, doc_form="text_model", max_length=4000)
    assert "parser" not in rules  # 默认值不写进规则
    with pytest.raises(Invalid):
        ip.normalize_process_rule(
            "custom", {"parser": "docling"}, doc_form="text_model", max_length=4000
        )
    plain = parse("员工手册.pdf", employee_handbook(), "text")
    assert plain.is_plain_text and plain.meta["parser"] == "text"
    assert [b.page for b in plain.blocks] == [1, 2, 3]
    assert parse("员工手册.pdf", employee_handbook()).meta["parser"] != "text"


# ───────────────────────────────────────────── 解析异常 → 待审核


async def test_parse_problems_go_to_review(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    doc = await _upload(client, rt, ds["id"], partly_scanned(), "报销.pdf")
    info = (
        await client.get(f"/v1/datasets/{ds['id']}/documents/{doc['id']}", headers=ALICE)
    ).json()
    assert info["indexing_status"] == "completed"
    assert info["review_status"] == "pending" and info["available"] is False
    assert "OCR" in info["review_note"] and info["parse_report"]["needs_review"] is True

    async def hits() -> int:
        r = await client.post(
            f"/v1/datasets/{ds['id']}/hit-testing", json={"query": "报销发票"}, headers=ALICE
        )
        return len(r.json()["records"])

    assert await hits() == 0  # 待审核的不参与检索
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents-review",
        json={"document_ids": [doc["id"]], "approve": True},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    assert await hits() > 0

    # 正常文档不进审核
    ok = await _upload(client, rt, ds["id"], employee_handbook(), "手册.pdf")
    info = (await client.get(f"/v1/datasets/{ds['id']}/documents/{ok['id']}", headers=ALICE)).json()
    assert info["review_status"] == "none"


# ───────────────────────────────────────────── 向量化用量进追踪


class UsageEmbedding(FakeEmbedding):
    provider = "openai_compatible"
    model = "usage-embedding"

    def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return self.embed_documents(texts), sum(len(t) for t in texts)


async def test_embedding_tokens_recorded_on_span(rt: Any) -> None:
    from atlas_knowledge.db.session import get_sessionmaker
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("t")
    emb = CacheEmbedding(UsageEmbedding())
    async with get_sessionmaker()() as s:
        with tracer.start_as_current_span("load") as span:
            await emb.embed_documents(s, ["一二三", "四五"], batch=1)
            await emb.embed_documents(s, ["一二三"], batch=1)  # 命中缓存，不再计费
        await s.rollback()
    assert span.attributes["knowledge.embedding.tokens"] == 5  # type: ignore[attr-defined]
    assert span.attributes["knowledge.embedding.cached"] == 1  # type: ignore[attr-defined]


# ───────────────────────────────────────────── 自适应多查询


class ScriptedChat:
    model = "fake-llm"

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls += 1
        return json.dumps(self.reply, ensure_ascii=False)


async def test_adaptive_multi_query(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], "差旅住宿标准每晚五百元。".encode(), "a.txt")
    rt.llm = ScriptedChat({"queries": ["出差住宿标准", "酒店报销上限"]})
    base = {"search_method": "semantic_search", "top_k": 3}

    async def hit(cfg: dict[str, Any]) -> dict[str, Any]:
        r = await client.post(
            f"/v1/datasets/{ds['id']}/hit-testing",
            json={"query": "差旅住宿报多少", "retrieval_config": cfg},
            headers=ALICE,
        )
        assert r.status_code == 200, r.text
        return r.json()

    first = (await hit(base))["records"][0]["score"]
    # 首轮分数高于阈值：不触发
    res = await hit({**base, "adaptive_multi_query_threshold": first / 2})
    assert "query_plan" not in res and rt.llm.calls == 0
    # 首轮分数低于阈值：生成多个表述再检索一次
    res = await hit({**base, "adaptive_multi_query_threshold": min(0.99, first + 0.05)})
    assert rt.llm.calls == 1
    assert res["query_plan"]["queries"] == ["出差住宿标准", "酒店报销上限"]
    assert res["query_plan"]["adaptive"] == round(first, 4)
    funnel = (await client.get(f"/v1/retrievals/{res['retrieval_id']}", headers=ALICE)).json()
    names = [s["name"] for s in funnel["stages"]]
    assert names.count("final") == 1 and names.count("semantic") == 3


# ───────────────────────────────────────────── 分段级元数据覆盖


async def test_segment_metadata_override(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    base = f"/v1/datasets/{ds['id']}"
    await client.post(
        f"{base}/metadata-fields",
        json={"name": "region", "type": "enum", "options": ["华东", "华北"]},
        headers=ALICE,
    )
    r = await client.post(
        f"{base}/documents",
        files=[("files", ("a.txt", "上海报销标准。\n\n北京报销标准。".encode(), "text/plain"))],
        data={"process_rule": json.dumps(CUSTOM), "metadata": json.dumps({"region": "华东"})},
        headers=ALICE,
    )
    doc = r.json()["documents"][0]
    await run_indexing(rt)
    segs = (await client.get(f"{base}/documents/{doc['id']}/segments", headers=ALICE)).json()[
        "data"
    ]
    bj = next(s for s in segs if s["content"].startswith("北京"))
    r = await client.patch(
        f"{base}/documents/{doc['id']}/segments/{bj['id']}",
        json={"metadata": {"region": "华北"}},
        headers=ALICE,
    )
    assert r.status_code == 200 and r.json()["metadata"] == {"region": "华北"}

    async def contents(region: str) -> list[str]:
        r = await client.post(
            f"{base}/hit-testing",
            json={"query": "报销标准", "filter": {"field": "region", "value": region}},
            headers=ALICE,
        )
        return sorted(x["content"] for x in r.json()["records"])

    assert await contents("华北") == ["北京报销标准。"]
    assert await contents("华东") == ["上海报销标准。"]
    # 文档元数据再改：分段覆盖仍然保留
    await client.patch(
        f"{base}/documents/{doc['id']}", json={"metadata": {"region": "华东"}}, headers=ALICE
    )
    assert await contents("华北") == ["北京报销标准。"]
    # 去掉覆盖：回到文档的元数据
    await client.patch(
        f"{base}/documents/{doc['id']}/segments/{bj['id']}", json={"metadata": None}, headers=ALICE
    )
    assert await contents("华北") == []
