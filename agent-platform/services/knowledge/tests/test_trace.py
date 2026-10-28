"""可观测性：检索过程记录（漏斗）与链路（span 树、traceparent 串联、正文档位）。"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_api import ALICE, CUSTOM, _dataset, _upload

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN = "00f067aa0ba902b7"


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """本地 TracerProvider（不动全局的）：把各模块取 tracer 的入口换掉。"""
    import atlas_knowledge.indexing.runner as runner
    import atlas_knowledge.rag.recorder as recorder
    import atlas_knowledge.services.retrieval as service
    import atlas_knowledge.telemetry as telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    for mod in (telemetry, recorder, service, runner):
        monkeypatch.setattr(mod, "tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(telemetry, "_capture", "io")
    return exporter


async def _hit_raw(client: Any, ds_id: str, query: str, **cfg: Any) -> dict[str, Any]:
    r = await client.post(
        f"/v1/datasets/{ds_id}/hit-testing",
        json={"query": query, "retrieval_config": cfg},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_funnel_truncated_and_threshold(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], process_rule=CUSTOM)

    # 混合检索、top_k=2：入选 2 个，其余（两路召回不重合的部分）是「排名靠后被截断」
    res = await _hit_raw(client, ds["id"], "怎么申请退款", search_method="hybrid_search", top_k=2)
    assert res["retrieval_id"]
    r = await client.get(f"/v1/retrievals/{res['retrieval_id']}", headers=ALICE)
    assert r.status_code == 200, r.text
    f = r.json()
    names = [s["name"] for s in f["stages"]]
    assert names == ["semantic", "full_text", "merged", "rerank", "assemble", "final"], names
    assert f["stages"][0]["dataset_uuid"] == ds["id"] and f["stages"][-1]["dataset_uuid"] is None
    top = f["rows"][0]
    assert top["status"] == "selected" and top["final_rank"] == 1
    assert top["content"].startswith("退款政策") and top["document_name"] == "faq.txt"
    assert [x["final_rank"] for x in f["rows"][:2]] == [1, 2]
    others = f["rows"][2:]
    assert all(o["status"] == "truncated" for o in others), others
    assert all(o["dropped_at"] == names.index("final") for o in others)
    # 矩阵与环节对齐；最终那一列只有入选的
    assert len(top["cells"]) == len(names)
    assert all(o["cells"][-1] is None for o in others)

    # 阈值卡在第一、二名之间：其余是「被阈值过滤」
    full = await _hit_raw(client, ds["id"], "怎么申请退款", search_method="hybrid_search")
    s1, s2 = full["records"][0]["score"], full["records"][1]["score"]
    res = await _hit_raw(
        client,
        ds["id"],
        "怎么申请退款",
        search_method="hybrid_search",
        score_threshold_enabled=True,
        score_threshold=(s1 + s2) / 2,
    )
    f = (await client.get(f"/v1/retrievals/{res['retrieval_id']}", headers=ALICE)).json()
    assert "threshold" in [s["name"] for s in f["stages"]]
    assert f["rows"][0]["status"] == "selected"
    assert {o["status"] for o in f["rows"][1:]} == {"below_threshold"}

    # 检索记录指回过程记录；别人看不到
    q = (await client.get(f"/v1/datasets/{ds['id']}/queries", headers=ALICE)).json()["data"]
    assert q[0]["retrieval_id"] == res["retrieval_id"]
    # dev 模式下人人有 kb:manage_all —— 直接用没有操作码的主体验证
    from uuid import UUID

    from atlas_knowledge.auth import Principal
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.errors import NotFound
    from atlas_knowledge.services.retrieval import KnowledgeRetrieval

    bob = Principal(uid="bob", operations=frozenset())
    async with get_sessionmaker()() as s:
        with pytest.raises(NotFound):
            await KnowledgeRetrieval(s, rt).get(bob, UUID(res["retrieval_id"]))


async def test_multi_dataset_stages(client: Any, rt: Any) -> None:
    a = await _dataset(client, name="A")
    b = await _dataset(client, name="B")
    await _upload(
        client,
        rt,
        a["id"],
        text="退款政策：七天内可以退款。\n\n发票自动开具。",
        process_rule=CUSTOM,
    )
    await _upload(
        client, rt, b["id"], text="请假需要提前一天申请。\n\n报销每周三处理。", process_rule=CUSTOM
    )
    r = await client.post(
        "/internal/v1/retrieve",
        json={
            "dataset_ids": [a["id"], b["id"]],
            "query": "请假",
            "source_ref": "thread-1",
            "retrieval_config": {"top_k": 1},
        },
        headers=ALICE,
    )
    rid = r.json()["retrieval_id"]
    f = (await client.get(f"/v1/retrievals/{rid}", headers=ALICE)).json()
    per_ds = [(s["name"], s["dataset_name"]) for s in f["stages"] if s["dataset_uuid"]]
    assert per_ds == [("semantic", "A"), ("semantic", "B")]
    assert [s["name"] for s in f["stages"] if not s["dataset_uuid"]] == [
        "merged",
        "assemble",
        "final",
    ]
    assert f["rows"][0]["dataset_name"] == "B" and f["rows"][0]["status"] == "selected"
    # 每库各召回 1 个、合并后只留 1 个：A 的那个是「排名靠后被截断」
    assert [(x["dataset_name"], x["status"]) for x in f["rows"][1:]] == [("A", "truncated")]


async def test_trace_propagation_and_content(
    client: Any, rt: Any, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], process_rule=CUSTOM)
    ingest = {s.name for s in spans.get_finished_spans()}
    assert {"knowledge.ingest", "knowledge.ingest.parse", "knowledge.ingest.load"} <= ingest
    spans.clear()

    monkeypatch.setattr(rt.settings, "trace_url_template", "http://jaeger/trace/{trace_id}")
    r = await client.post(
        f"/v1/datasets/{ds['id']}/hit-testing",
        json={"query": "怎么申请退款", "retrieval_config": {"search_method": "hybrid_search"}},
        headers={**ALICE, "traceparent": f"00-{TRACE_ID}-{PARENT_SPAN}-01"},
    )
    assert r.json()["trace_id"] == TRACE_ID

    got = {s.name: s for s in spans.get_finished_spans()}
    assert all(format(s.context.trace_id, "032x") == TRACE_ID for s in got.values())
    server = got[f"POST /v1/datasets/{ds['id']}/hit-testing"]
    assert format(server.parent.span_id, "016x") == PARENT_SPAN
    root = got["knowledge.retrieve"]
    assert root.parent.span_id == server.context.span_id
    for name in ("knowledge.semantic", "knowledge.full_text", "knowledge.rerank"):
        assert got[name].parent.span_id == root.context.span_id, name
    assert got["knowledge.embed_query"].attributes["openinference.span.kind"] == "EMBEDDING"
    assert got["knowledge.rerank"].attributes["knowledge.candidates"] >= 1

    # io 档：根 span 带查询与结果正文；各环节候选正文（full 档）不带
    assert root.attributes["langfuse.observation.input"] == "怎么申请退款"
    assert "退款政策" in root.attributes["langfuse.observation.output"]
    assert "langfuse.observation.output" not in got["knowledge.rerank"].attributes

    f = (await client.get(f"/v1/retrievals/{r.json()['retrieval_id']}", headers=ALICE)).json()
    assert f["trace_id"] == TRACE_ID and f["trace_url"] == f"http://jaeger/trace/{TRACE_ID}"
