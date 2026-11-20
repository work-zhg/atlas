"""多租户：库归租户、入库任务进入文档所属的租户、运营接口（停用 / 恢复 / 删除租户）。

c4-model/opscenter/design-tenant-isolation.html §8。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
from atlas_knowledge.db.models import Dataset, Document
from atlas_knowledge.db.session import get_sessionmaker
from atlas_knowledge.indexing.runner import IndexingRunner, recover_stalled, run_queued
from atlas_tenancy import TenantScope
from conftest import OTHER_TENANT, TEST_TENANT, ops_request, run_indexing
from sqlalchemy import select

ALICE = {"X-Uid": "alice"}
BOB_ELSEWHERE = {"X-Uid": "bob", "X-Tenant-Id": OTHER_TENANT}
FAQ = "退款政策：购买后七天内可以申请无理由退款。\n\n发票在订单页申请。"
CUSTOM = {
    "mode": "custom",
    "rules": {"segmentation": {"separator": "\\n\\n", "max_tokens": 200, "chunk_overlap": 0}},
}


async def _dataset(client: Any, headers: dict[str, str] = ALICE, **body: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/datasets",
        json={"name": "客服知识库", "embedding_model": "fake-embedding", **body},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def _upload(client: Any, ds_id: str, headers: dict[str, str] = ALICE) -> dict[str, Any]:
    r = await client.post(
        f"/v1/datasets/{ds_id}/documents",
        files=[("files", ("faq.txt", FAQ.encode(), "text/plain"))],
        data={"process_rule": json.dumps(CUSTOM)},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["documents"][0]  # type: ignore[no-any-return]


async def _status(doc_uuid: str, tenant: str = TEST_TENANT) -> str:
    with TenantScope.enter(UUID(tenant)):
        async with get_sessionmaker()() as s:
            doc = await s.scalar(select(Document).where(Document.uuid == UUID(doc_uuid)))
            return "gone" if doc is None else doc.indexing_status


async def _ops(client: Any, action: str, tenant: str = TEST_TENANT) -> httpx.Response:
    path = f"/ops/v1/tenants/{tenant}/{action}"
    return await client.request("POST", path, **ops_request("POST", path))  # type: ignore[no-any-return]


# ───────────────────────────────────────────── 库归租户


async def test_all_datasets_owner_sees_only_his_tenant(client: Any, rt: Any) -> None:
    """「全部知识库 · 所有者」到这里已经只剩本租户的库了：别的租户的库查不到、检索不到。"""
    ds = await _dataset(client)
    await _upload(client, ds["id"])
    await run_indexing(rt)
    rt.perms.type_levels["bob"] = "OWNER"

    listed = (await client.get("/v1/datasets", headers=BOB_ELSEWHERE)).json()
    assert listed["data"] == []
    assert (await client.get(f"/v1/datasets/{ds['id']}", headers=BOB_ELSEWHERE)).status_code == 404
    r = await client.post(
        "/internal/v1/retrieve",
        json={"dataset_ids": [ds["id"]], "query": "怎么退款"},
        headers=BOB_ELSEWHERE,
    )
    assert r.status_code == 200 and r.json()["records"] == []

    # 同一个租户里他就是所有者
    same = (await client.get("/v1/datasets", headers={"X-Uid": "bob"})).json()
    assert [d["id"] for d in same["data"]] == [ds["id"]]


async def test_tenant_header_is_required_and_checked(client: Any, monkeypatch: Any) -> None:
    from atlas_knowledge import auth

    bare = httpx.AsyncClient(transport=client._transport, base_url="http://kb")
    async with bare:
        r = await bare.get("/v1/datasets", headers=ALICE)
        assert r.status_code == 401 and "X-Tenant-Id" in r.json()["detail"]
        r = await bare.post("/internal/v1/retrieve", json={"dataset_ids": [str(UUID(int=1))]})
        assert r.status_code == 401

    # 用户中心说他在别的租户：请求头写的租户不认
    def elsewhere(req: httpx.Request) -> httpx.Response:
        user = {"status": "active", "tenant_id": OTHER_TENANT}
        return httpx.Response(200, json={"can_access": True, "permissions": [], "user": user})

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(elsewhere))
    monkeypatch.setattr(auth, "_cache", auth._Cache())
    r = await client.get("/v1/datasets", headers=ALICE)
    assert r.status_code == 401 and "不一致" in r.json()["detail"]


async def test_files_live_under_the_tenant(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    doc = await _upload(client, ds["id"])
    async with get_sessionmaker()() as s:
        row = await s.scalar(select(Dataset).where(Dataset.uuid == UUID(ds["id"])))
        assert row is not None
        assert row.storage_prefix == f"{TEST_TENANT}/_knowledge/{ds['id']}"
    keys = list(rt.storage.objects)
    assert keys == [f"{TEST_TENANT}/_knowledge/{ds['id']}/{doc['id']}/faq.txt"]


async def test_worker_enters_the_documents_tenant(client: Any, rt: Any) -> None:
    """任务消息里只有文档 id：worker 从文档行取租户，不继承投递方的上下文。"""
    ds = await _dataset(client)
    doc = await _upload(client, ds["id"])
    queued, rt.dispatcher.queued = list(rt.dispatcher.queued), []
    with TenantScope.isolated():
        assert await run_queued(rt, get_sessionmaker(), queued) == []
    assert await _status(doc["id"]) == "completed"


# ───────────────────────────────────────────── 停用 / 恢复


async def test_suspended_tenant_is_shut_out_and_its_ingest_waits(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    doc = await _upload(client, ds["id"])
    queued, rt.dispatcher.queued = list(rt.dispatcher.queued), []

    assert (await _ops(client, "suspend")).status_code == 200
    r = await client.get("/v1/datasets", headers=ALICE)
    assert r.status_code == 403 and "停用" in r.json()["detail"]
    assert (await client.post("/internal/v1/datasets/readable", json={})).status_code == 403
    # 别的租户照常
    assert (await client.get("/v1/datasets", headers=BOB_ELSEWHERE)).status_code == 200

    # 入库不开始：文档留在排队中，交回给任务过一会儿再投
    rt.tenant_gate = client._transport.app.state.tenant_gate
    with TenantScope.isolated():
        assert await run_queued(rt, get_sessionmaker(), queued) == queued
    assert await _status(doc["id"]) == "waiting"

    # 巡检也跳过它（停在中间状态、没人持锁的文档）
    async with get_sessionmaker()() as s:
        row = await s.scalar(select(Document).where(Document.uuid == UUID(doc["id"])))
        assert row is not None
        row.indexing_status = "indexing"
        await s.commit()
    with TenantScope.isolated():
        assert await recover_stalled(rt, get_sessionmaker()) == []

    assert (await _ops(client, "resume")).status_code == 200
    with TenantScope.isolated():
        assert await recover_stalled(rt, get_sessionmaker()) == [doc["id"]]
    await IndexingRunner(rt, get_sessionmaker()).run(doc["id"])
    assert await _status(doc["id"]) == "completed"
    assert (await client.get("/v1/datasets", headers=ALICE)).status_code == 200


# ───────────────────────────────────────────── 删除


async def test_purge_deletes_only_this_tenants_rows_vectors_and_files(client: Any, rt: Any) -> None:
    mine = await _dataset(client)
    await _upload(client, mine["id"])
    theirs = await _dataset(client, headers=BOB_ELSEWHERE)
    await _upload(client, theirs["id"], headers=BOB_ELSEWHERE)
    await run_indexing(rt)
    # 多租户之前建的库：文件在桶根 _knowledge/{库}
    async with get_sessionmaker()() as s:
        row = await s.scalar(select(Dataset).where(Dataset.uuid == UUID(mine["id"])))
        assert row is not None
        row.storage_prefix = f"_knowledge/{mine['id']}"
        await s.commit()
    rt.storage.put(f"_knowledge/{mine['id']}/old/faq.txt", b"old")
    # 同一个桶里别的服务的文件（Atlas 的工作空间）
    rt.storage.put(f"{TEST_TENANT}/workspace/a.txt", b"atlas")
    async with get_sessionmaker()() as s:
        names = {d.collection_name for d in await s.scalars(select(Dataset))}
    with TenantScope.enter(UUID(OTHER_TENANT)):
        async with get_sessionmaker()() as s:
            other_names = {d.collection_name for d in await s.scalars(select(Dataset))}

    assert (await _ops(client, "suspend")).status_code == 200
    r = await _ops(client, "purge")
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] is False

    keys = set(rt.storage.objects)
    assert not any(k.startswith(f"{TEST_TENANT}/_knowledge/") for k in keys)
    assert not any(k.startswith(f"_knowledge/{mine['id']}/") for k in keys)
    assert f"{TEST_TENANT}/workspace/a.txt" in keys, "只删知识库自己的子目录"
    assert any(k.startswith(f"{OTHER_TENANT}/_knowledge/{theirs['id']}/") for k in keys)
    client_ = rt.vectors._client
    for name in names:
        assert not await client_.collection_exists(name)
    for name in other_names:
        assert await client_.collection_exists(name)
    async with get_sessionmaker()() as s:
        assert list(await s.scalars(select(Dataset))) == []
    with TenantScope.enter(UUID(OTHER_TENANT)):
        async with get_sessionmaker()() as s:
            assert [str(d.uuid) for d in await s.scalars(select(Dataset))] == [theirs["id"]]
