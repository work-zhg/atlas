"""向量化与索引（doc/knowledge-enterprise-design.html §6）：远程模型、索引版本与原子切换、
payload 扩展。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from atlas_knowledge.db.models import Segment
from atlas_knowledge.models.remote_models import OpenAICompatibleEmbedding, RemoteReranker
from conftest import FakeEmbedding, run_indexing, run_rebuilds

ALICE = {"X-Uid": "alice"}
CUSTOM = {
    "mode": "custom",
    "rules": {"segmentation": {"separator": "\\n\\n", "max_tokens": 200, "chunk_overlap": 0}},
}


class FakeEmbedding2(FakeEmbedding):
    """另一个模型：维度不同（换模型必须新建 collection）。"""

    model = "fake-embedding-2"
    dim = 128


# ───────────────────────────────────────────── 远程模型


def test_openai_compatible_embedding_batches_retries_and_counts_tokens() -> None:
    calls: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 2:  # 第二次请求先限流一次
            return httpx.Response(429)
        data = [
            {"index": i, "embedding": [float(len(t)), 1.0, 0.0]}
            for i, t in reversed(list(enumerate(body["input"])))  # 乱序返回也要按 index 对齐
        ]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 7}})

    m = OpenAICompatibleEmbedding(
        "bge-m3", base_url="http://gw/v1", transport=httpx.MockTransport(handler)
    )
    assert m.dim == 3 and m.provider == "openai_compatible"
    import atlas_knowledge.models.remote_models as rm

    rm.time.sleep = lambda _s: None  # type: ignore[assignment]
    assert m.embed_documents(["a", "bbb"]) == [[1.0, 1.0, 0.0], [3.0, 1.0, 0.0]]
    assert calls[-1] == {"model": "bge-m3", "input": ["a", "bbb"]}
    assert m.tokens == 14


@pytest.mark.parametrize("provider", ["cohere_compatible", "tei"])
def test_remote_reranker(provider: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if provider == "tei":
            assert body["texts"] == ["x", "y"]
            return httpx.Response(
                200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}]
            )
        assert body["documents"] == ["x", "y"] and body["model"] == "bge-reranker-v2-m3"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    r = RemoteReranker(
        "bge-reranker-v2-m3",
        provider=provider,
        base_url="http://rr",
        transport=httpx.MockTransport(handler),
    )
    scores = r.rerank("q", ["x", "y"])
    assert scores[1] > scores[0]


# ───────────────────────────────────────────── 索引版本：双写、重建、切换、回滚


async def _dataset(client: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/datasets", json={"name": "制度库", "embedding_model": "fake-embedding"}, headers=ALICE
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _upload(client: Any, rt: Any, ds_id: str, text: str, name: str) -> dict[str, Any]:
    r = await client.post(
        f"/v1/datasets/{ds_id}/documents",
        files=[("files", (name, text.encode(), "text/plain"))],
        data={"process_rule": json.dumps(CUSTOM)},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    await run_indexing(rt)
    return r.json()["documents"][0]


async def _hit(client: Any, ds_id: str, query: str, version: str | None = None) -> list[str]:
    body: dict[str, Any] = {
        "query": query,
        "retrieval_config": {"search_method": "semantic_search", "top_k": 3},
    }
    if version:
        body["index_version_id"] = version
    r = await client.post(f"/v1/datasets/{ds_id}/hit-testing", json=body, headers=ALICE)
    assert r.status_code == 200, r.text
    return [rec["content"] for rec in r.json()["records"]]


async def test_index_version_rebuild_switch_and_rollback(client: Any, rt: Any) -> None:
    rt.registry.register_embedding(FakeEmbedding2())
    ds = await _dataset(client)
    base = f"/v1/datasets/{ds['id']}"
    await _upload(client, rt, ds["id"], "请假需要提前一天申请。\n\n报销每周三处理。", "a.txt")

    r = await client.post(
        f"{base}/index-versions",
        json={"embedding_model": "fake-embedding-2"},
        headers=ALICE,
    )
    assert r.status_code == 202, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["status"] == "building" and v2["embedding_dim"] == 128
    # 同一时间只能有一个构建中的版本
    r = await client.post(
        f"{base}/index-versions", json={"embedding_model": "fake-embedding-2"}, headers=ALICE
    )
    assert r.status_code == 409

    # 构建期间入库的文档双写进新版本
    await _upload(client, rt, ds["id"], "年假按工龄计算，满一年五天。", "b.txt")
    await run_rebuilds(rt)

    versions = (await client.get(f"{base}/index-versions", headers=ALICE)).json()["data"]
    assert [(v["version"], v["status"]) for v in versions] == [(2, "ready"), (1, "active")]
    assert versions[0]["progress"]["documents"] == 2
    # 切换前用新版本检索对比；默认仍是旧版本
    assert (await _hit(client, ds["id"], "年假几天", v2["id"]))[0].startswith("年假")
    assert (await client.get(base, headers=ALICE)).json()["embedding_model"] == "fake-embedding"

    r = await client.post(f"{base}/index-versions/{v2['id']}/activate", headers=ALICE)
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert (await client.get(base, headers=ALICE)).json()["embedding_model"] == "fake-embedding-2"
    assert (await _hit(client, ds["id"], "请假要提前多久"))[0].startswith("请假")

    # 切换后入库仍写进退役的旧版本（保留用于回滚），回滚后数据完整
    await _upload(client, rt, ds["id"], "加班需要部门经理审批。", "c.txt")
    v1 = next(v for v in versions if v["version"] == 1)
    r = await client.post(f"{base}/index-versions/{v1['id']}/activate", headers=ALICE)
    assert r.status_code == 200, r.text
    assert (await _hit(client, ds["id"], "加班谁审批"))[0].startswith("加班")

    # 不能删 active 版本；删掉退役版本后它的 collection 就没了，不再双写
    assert (
        await client.delete(f"{base}/index-versions/{v1['id']}", headers=ALICE)
    ).status_code == 400
    assert (
        await client.delete(f"{base}/index-versions/{v2['id']}", headers=ALICE)
    ).status_code == 204
    v2_store = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}_v2")
    assert await v2_store.point_ids() == []
    await _upload(client, rt, ds["id"], "出差住宿标准每晚五百元。", "d.txt")
    assert await v2_store.point_ids() == []


async def test_changing_embedding_model_rebuilds_then_switches(client: Any, rt: Any) -> None:
    rt.registry.register_embedding(FakeEmbedding2())
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], "请假需要提前一天申请。", "a.txt")
    r = await client.patch(
        f"/v1/datasets/{ds['id']}", json={"embedding_model": "fake-embedding-2"}, headers=ALICE
    )
    assert r.status_code == 200, r.text
    assert r.json()["embedding_model"] == "fake-embedding"  # 构建完成前不变
    await run_rebuilds(rt)
    got = (await client.get(f"/v1/datasets/{ds['id']}", headers=ALICE)).json()
    assert got["embedding_model"] == "fake-embedding-2"
    assert (await _hit(client, ds["id"], "请假"))[0].startswith("请假")


async def test_rebuild_removes_orphans(client: Any, rt: Any, monkeypatch: Any) -> None:
    """竞态：重建读到分段并写完向量之后，分段被删（删除时新 collection 里还没有这些 point）。
    重建收尾时清掉新 collection 里库中已不存在的 point。"""
    from atlas_knowledge.index import loader
    from sqlalchemy import delete

    rt.registry.register_embedding(FakeEmbedding2())
    ds = await _dataset(client)
    base = f"/v1/datasets/{ds['id']}"
    await _upload(client, rt, ds["id"], "请假需要提前一天申请。", "a.txt")
    await client.post(
        f"{base}/index-versions", json={"embedding_model": "fake-embedding-2"}, headers=ALICE
    )
    real = loader.index_segments

    async def write_then_lose(s: Any, *args: Any, **kwargs: Any) -> int:
        n = await real(s, *args, **kwargs)
        await s.execute(delete(Segment))
        return n

    monkeypatch.setattr(loader, "index_segments", write_then_lose)
    await run_rebuilds(rt)
    got = (await client.get(f"{base}/index-versions", headers=ALICE)).json()["data"][0]
    assert got["status"] == "ready" and got["progress"]["orphans_removed"] == 1
    v2_store = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}_v2")
    assert await v2_store.point_ids() == []


# ───────────────────────────────────────────── payload 扩展


async def test_payload_has_doc_fields_and_follows_status(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    doc = await _upload(client, rt, ds["id"], "请假需要提前一天申请。", "a.txt")
    store = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}")
    hits = await store.search_by_vector(FakeEmbedding().embed_query("请假"), top_k=1)
    payload = hits[0].payload
    assert payload["kind"] == "segment" and "acl" not in payload
    assert payload["is_current"] is True
    assert payload["meta"]["doc_type"] == "txt" and payload["meta"]["author"] == "alice"
    assert "valid_to" not in payload

    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents-status/disable",
        json={"document_ids": [doc["id"]]},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    assert await store.search_by_vector(FakeEmbedding().embed_query("请假"), top_k=1) == []


async def test_completion_resyncs_doc_fields_changed_mid_ingest(
    client: Any, rt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入库中途改了文档元数据（改字段时那一批还没写进向量库）：完成时按文档再同步一次，不留旧值。"""
    import atlas_knowledge.document.runner as runner_mod
    from atlas_knowledge.db.models import Document
    from atlas_knowledge.db.session import get_sessionmaker
    from sqlalchemy import select

    original = runner_mod.index_segments

    async def write_then_change_meta(session: Any, rt_: Any, ds: Any, doc: Any, segs: Any) -> int:
        n = await original(session, rt_, ds, doc, segs)  # 这一批带着旧元数据写进去
        async with get_sessionmaker()() as s:  # 同时另一个请求改了元数据
            row = await s.scalar(select(Document).where(Document.uuid == doc.uuid))
            assert row is not None
            row.doc_metadata = {"region": "华东"}
            await s.commit()
        return n

    monkeypatch.setattr(runner_mod, "index_segments", write_then_change_meta)
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], "请假需要提前一天申请。", "a.txt")
    store = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}")
    hits = await store.search_by_vector(FakeEmbedding().embed_query("请假"), top_k=1)
    assert hits and hits[0].payload["meta"]["region"] == "华东"


# ───────────────────────────────────────────── 重建可续跑


async def _set_status(doc_id: str, status: str) -> None:
    from uuid import UUID

    from atlas_knowledge.db.models import Document
    from atlas_knowledge.db.session import get_sessionmaker
    from sqlalchemy import select

    async with get_sessionmaker()() as s:
        doc = await s.scalar(select(Document).where(Document.uuid == UUID(doc_id)))
        assert doc is not None
        doc.indexing_status = status
        await s.commit()


async def _new_version(client: Any, ds_id: str) -> dict[str, Any]:
    r = await client.post(
        f"/v1/datasets/{ds_id}/index-versions",
        json={"embedding_model": "fake-embedding-2"},
        headers=ALICE,
    )
    assert r.status_code == 202, r.text
    return r.json()


async def _version(client: Any, ds_id: str, version_id: str) -> dict[str, Any]:
    got = (await client.get(f"/v1/datasets/{ds_id}/index-versions", headers=ALICE)).json()
    return next(v for v in got["data"] if v["id"] == version_id)


async def test_rebuild_resumes_after_the_worker_dies(
    client: Any, rt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重建中途进程被杀：版本停在 building、锁随进程消失；巡检重新投递，从记下的进度续跑，
    已重写的文档不再重写。"""
    import asyncio

    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.index import versions as versions_mod
    from atlas_knowledge.index.versions import recover_rebuilds

    rt.registry.register_embedding(FakeEmbedding2())
    ds = await _dataset(client)
    for i, text in enumerate(["请假需要提前一天申请。", "报销每周三处理。", "年假满一年五天。"]):
        await _upload(client, rt, ds["id"], text, f"{i}.txt")
    v2 = await _new_version(client, ds["id"])

    real = versions_mod._rewrite
    rewritten: list[str] = []

    async def die_on_second(*args: Any, **kwargs: Any) -> int:
        if len(rewritten) == 1:
            raise asyncio.CancelledError  # 进程被杀：不是普通异常，不会把版本标成失败
        rewritten.append(str(args[3]))
        return await real(*args, **kwargs)

    monkeypatch.setattr(versions_mod, "_rewrite", die_on_second)
    with pytest.raises(asyncio.CancelledError):
        await run_rebuilds(rt)
    got = await _version(client, ds["id"], v2["id"])
    assert got["status"] == "building" and got["progress"]["documents"] == 1

    monkeypatch.setattr(versions_mod, "_rewrite", real)
    assert await recover_rebuilds(rt, get_sessionmaker()) == [v2["id"]]
    await run_rebuilds(rt)
    got = await _version(client, ds["id"], v2["id"])
    assert got["status"] == "ready" and got["progress"]["documents"] == 3
    v2_store = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}_v2")
    assert len(await v2_store.point_ids()) == 3
    # 没有 building 的版本，巡检什么都不投
    assert await recover_rebuilds(rt, get_sessionmaker()) == []


async def test_rebuild_waits_for_documents_writing_when_the_version_was_created(
    client: Any, rt: Any
) -> None:
    """版本建立这一刻还在写向量的文档，前几批没进新 collection：重建等它停下再整篇补写。"""
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.index.versions import recover_rebuilds

    rt.registry.register_embedding(FakeEmbedding2())
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], "请假需要提前一天申请。", "a.txt")
    late = await _upload(client, rt, ds["id"], "报销每周三处理。", "b.txt")
    paused = await _upload(client, rt, ds["id"], "年假满一年五天。", "c.txt")
    # b 还在向量化、c 写了一部分后暂停（它们的分段都已写进旧版本）
    await _set_status(late["id"], "indexing")
    await _set_status(paused["id"], "paused")
    v2 = await _new_version(client, ds["id"])
    assert sorted(v2["progress"]["early"]) == sorted([late["id"], paused["id"]])

    await run_rebuilds(rt)
    got = await _version(client, ds["id"], v2["id"])
    # 暂停的补写了已写过的分段；还在写的要等
    assert got["status"] == "building" and got["progress"]["early"] == [late["id"]]
    v2_store = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}_v2")
    assert len(await v2_store.point_ids()) == 2

    await _set_status(late["id"], "completed")
    assert await recover_rebuilds(rt, get_sessionmaker()) == [v2["id"]]
    await run_rebuilds(rt)
    got = await _version(client, ds["id"], v2["id"])
    assert got["status"] == "ready" and got["progress"]["early"] == []
    assert len(await v2_store.point_ids()) == 3


async def test_new_collections_get_every_metadata_field_index(
    client: Any, rt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字段在第一次入库之前定义、或重建出新版本：collection 建的时候就带上全部字段的索引。"""
    from atlas_knowledge.index.vdb.qdrant import QdrantVector

    made: list[tuple[str, str]] = []
    real = QdrantVector.ensure_index

    async def spy(self: QdrantVector, key: str, kind: str) -> None:
        made.append((self.collection, key))
        await real(self, key, kind)

    monkeypatch.setattr(QdrantVector, "ensure_index", spy)
    rt.registry.register_embedding(FakeEmbedding2())
    ds = await _dataset(client)
    r = await client.post(
        f"/v1/datasets/{ds['id']}/metadata-fields",
        json={"name": "region", "type": "string"},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    await _upload(client, rt, ds["id"], "请假需要提前一天申请。", "a.txt")
    v1 = f"kb_{ds['id'].replace('-', '')}"
    assert (v1, "meta.region") in made
    await _new_version(client, ds["id"])
    await run_rebuilds(rt)
    assert (f"{v1}_v2", "meta.region") in made
