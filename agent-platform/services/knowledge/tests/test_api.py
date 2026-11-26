"""接口全流程：建库 → 上传 → 入库 → 召回测试 → 分段管理 → 状态 / 审核 → 权限。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from conftest import run_indexing

ALICE = {"X-Uid": "alice"}
BOB = {"X-Uid": "bob"}

FAQ = """退款政策：购买后七天内可以申请无理由退款，退款会在三个工作日内原路返回。

发票说明：电子发票在订单完成后自动开具，可以在个人中心下载。

请假制度：员工请假需要提前一天在系统中提交申请，由直属主管审批。

报销流程：差旅报销需要上传发票照片，财务部门每周三统一处理。"""


async def _dataset(client: Any, **body: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/datasets",
        json={"name": "客服知识库", "embedding_model": "fake-embedding", **body},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _upload(
    client: Any, rt: Any, ds_id: str, text: str = FAQ, name: str = "faq.txt", **form: Any
) -> dict[str, Any]:
    data = {k: json.dumps(v) if isinstance(v, dict) else v for k, v in form.items()}
    r = await client.post(
        f"/v1/datasets/{ds_id}/documents",
        files=[("files", (name, text.encode(), "text/plain"))],
        data=data,
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    await run_indexing(rt)
    return r.json()["documents"][0]


async def _hit(
    client: Any, ds_id: str, query: str, headers: dict[str, str] = ALICE, **cfg: Any
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"query": query}
    if cfg:
        body["retrieval_config"] = cfg
    r = await client.post(f"/v1/datasets/{ds_id}/hit-testing", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["records"]


CUSTOM = {
    "mode": "custom",
    "rules": {"segmentation": {"separator": "\\n\\n", "max_tokens": 200, "chunk_overlap": 0}},
}


async def test_full_flow(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    doc = await _upload(client, rt, ds["id"], process_rule=CUSTOM)

    r = await client.get(f"/v1/datasets/{ds['id']}/documents/{doc['id']}", headers=ALICE)
    got = r.json()
    assert got["indexing_status"] == "completed", got
    assert got["segment_count"] == 4 and got["completed_segments"] == 4
    assert got["process_rule"]["mode"] == "custom"

    # 语义 / 全文 / 混合（加权重排）都能找到退款那段
    for cfg in (
        {"search_method": "semantic_search"},
        {"search_method": "full_text_search"},
        {"search_method": "hybrid_search"},
        {
            "search_method": "semantic_search",
            "reranking_enable": True,
            "reranking_mode": "reranking_model",
            "reranking_model": {"provider": "fastembed", "model": "fake-reranker"},
        },
    ):
        records = await _hit(client, ds["id"], "怎么申请退款", **cfg)
        assert records and records[0]["content"].startswith("退款政策"), (cfg, records)
        assert records[0]["document_name"] == "faq.txt"

    # 检索记录
    r = await client.get(f"/v1/datasets/{ds['id']}/queries", headers=ALICE)
    assert len(r.json()["data"]) == 4

    # 分段：停用后检索不到；编辑内容后按新内容检索
    r = await client.get(f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments", headers=ALICE)
    segs = r.json()["data"]
    refund = next(s for s in segs if s["content"].startswith("退款政策"))
    # 命中次数从检索记录汇总：上面 4 次召回测试都命中了它
    assert refund["hit_count"] == 4
    base = f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments/{refund['id']}"
    assert (await client.patch(base, json={"enabled": False}, headers=ALICE)).status_code == 200
    records = await _hit(client, ds["id"], "怎么申请退款", search_method="full_text_search")
    assert all(not r["content"].startswith("退款政策") for r in records)
    await client.patch(
        base,
        json={"enabled": True, "content": "退货政策：收到商品十五天内可以退货。"},
        headers=ALICE,
    )
    records = await _hit(client, ds["id"], "收到商品多久可以退货", search_method="full_text_search")
    assert records[0]["content"].startswith("退货政策")

    # 新增分段
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments",
        json={"content": "会员积分可以抵扣运费。"},
        headers=ALICE,
    )
    assert r.status_code == 201
    records = await _hit(client, ds["id"], "积分抵扣运费", search_method="full_text_search")
    assert records[0]["content"].startswith("会员积分")

    # 删除分段
    assert (
        await client.delete(
            f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments/{r.json()['id']}",
            headers=ALICE,
        )
    ).status_code == 204
    records = await _hit(client, ds["id"], "积分抵扣运费", search_method="full_text_search")
    assert all(not r["content"].startswith("会员积分") for r in records)

    # 文档停用 → 检索不到；启用 → 恢复
    st = f"/v1/datasets/{ds['id']}/documents-status"
    await client.post(f"{st}/disable", json={"document_ids": [doc["id"]]}, headers=ALICE)
    assert await _hit(client, ds["id"], "发票", search_method="full_text_search") == []
    await client.post(f"{st}/enable", json={"document_ids": [doc["id"]]}, headers=ALICE)
    assert await _hit(client, ds["id"], "发票", search_method="full_text_search")

    # 删除文档 → 向量与原文件都清掉
    assert (
        await client.delete(f"/v1/datasets/{ds['id']}/documents/{doc['id']}", headers=ALICE)
    ).status_code == 204
    assert await _hit(client, ds["id"], "发票", search_method="full_text_search") == []
    assert rt.storage.objects == {}


async def test_parent_child(client: Any, rt: Any) -> None:
    ds = await _dataset(client, doc_form="hierarchical_model")
    rule = {
        "mode": "hierarchical",
        "rules": {
            "parent_mode": "paragraph",
            "segmentation": {"separator": "\\n\\n", "max_tokens": 500},
            "subchunk_segmentation": {"separator": "，", "max_tokens": 60},
        },
    }
    doc = await _upload(client, rt, ds["id"], process_rule=rule)
    segs = (
        await client.get(f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments", headers=ALICE)
    ).json()["data"]
    assert len(segs) == 4 and all(s["child_chunks"] for s in segs)

    records = await _hit(client, ds["id"], "退款原路返回", search_method="full_text_search")
    top = records[0]
    # 命中子块，返回整段父块 + 命中的子块
    assert top["content"].startswith("退款政策")
    assert top["child_chunks"] and "原路返回" in "".join(c["content"] for c in top["child_chunks"])

    # 改父分段 → 子块按规则重切
    seg = next(s for s in segs if s["content"].startswith("报销"))
    r = await client.patch(
        f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments/{seg['id']}",
        json={"content": "报销规则：餐费上限每天一百元，交通费实报实销，住宿按城市标准"},
        headers=ALICE,
    )
    assert len(r.json()["child_chunks"]) == 3
    records = await _hit(client, ds["id"], "交通费实报实销", search_method="full_text_search")
    assert records[0]["content"].startswith("报销规则")

    # 手工加子块
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents/{doc['id']}/segments/{seg['id']}/child_chunks",
        json={"content": "出差补贴每天五十元"},
        headers=ALICE,
    )
    assert r.status_code == 201
    records = await _hit(client, ds["id"], "出差补贴", search_method="full_text_search")
    assert records[0]["content"].startswith("报销规则")


async def test_estimate(client: Any) -> None:
    r = await client.post(
        "/v1/datasets/indexing-estimate",
        files={"file": ("faq.txt", FAQ.encode(), "text/plain")},
        data={"doc_form": "text_model", "process_rule": json.dumps(CUSTOM)},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_segments"] == 4 and body["preview"][0]["content"].startswith("退款政策")


async def test_pause_resume_and_retry(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents",
        files=[("files", ("faq.txt", FAQ.encode(), "text/plain"))],
        headers=ALICE,
    )
    doc = r.json()["documents"][0]
    base = f"/v1/datasets/{ds['id']}/documents/{doc['id']}"
    assert (await client.post(f"{base}/pause", headers=ALICE)).json()["indexing_status"] == "paused"
    await run_indexing(rt)  # 已投递的任务看到暂停标记就停
    assert (await client.get(base, headers=ALICE)).json()["indexing_status"] == "paused"
    assert (await client.post(f"{base}/resume", headers=ALICE)).status_code == 200
    await run_indexing(rt)
    assert (await client.get(base, headers=ALICE)).json()["indexing_status"] == "completed"

    # 解析失败 → error → 重试
    rt.storage.objects.clear()
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents",
        files=[("files", ("b.txt", b"hello world text", "text/plain"))],
        headers=ALICE,
    )
    bad = r.json()["documents"][0]
    rt.storage.objects.clear()  # 原文件丢了
    await run_indexing(rt)
    b = f"/v1/datasets/{ds['id']}/documents/{bad['id']}"
    got = (await client.get(b, headers=ALICE)).json()
    assert got["indexing_status"] == "error" and got["error"]
    assert (await client.post(f"{b}/retry", headers=ALICE)).json()["indexing_status"] == "waiting"


async def test_permissions(client: Any, rt: Any) -> None:
    """库级权限 = 用户中心数据权限（AtlasKnowledgeBase）。

    新建时按预设写初始授权，之后经共享接口调整。
    """
    private = await _dataset(client)
    shared = await _dataset(
        client, permission="partial_members", members=[{"id": "bob", "level": "read"}]
    )
    team = await _dataset(client, permission="all_team")

    async def ids(headers: dict[str, str]) -> set[str]:
        r = await client.get("/v1/datasets", headers=headers)
        return {d["id"] for d in r.json()["data"]}

    assert await ids(BOB) == {shared["id"], team["id"]}, "建库预设写进了用户中心"
    assert (await client.get(f"/v1/datasets/{private['id']}", headers=BOB)).status_code == 404
    me = (await client.get(f"/v1/datasets/{shared['id']}", headers=BOB)).json()
    assert me["my_level"] == "READ" and me["can_write"] is False and me["can_admin"] is False
    # 只读的人改不了、删不了、共享不了
    r = await client.patch(f"/v1/datasets/{shared['id']}", json={"name": "x"}, headers=BOB)
    assert r.status_code == 403
    assert (await client.delete(f"/v1/datasets/{shared['id']}", headers=BOB)).status_code == 403
    body = {"level": "WRITE", "subjects": [{"type": "user", "id": "carol"}]}
    r = await client.post(f"/v1/datasets/{shared['id']}/grants", json=body, headers=BOB)
    assert r.status_code == 403

    # 所有者共享：bob 升为读写；可见范围不再经 PATCH 改
    r = await client.post(
        f"/v1/datasets/{private['id']}/grants",
        json={"level": "WRITE", "subjects": [{"type": "user", "id": "bob"}]},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    assert private["id"] in await ids(BOB)
    assert (await client.get(f"/v1/datasets/{private['id']}", headers=BOB)).json()["can_write"]
    r = await client.patch(
        f"/v1/datasets/{private['id']}", json={"permission": "all_team"}, headers=ALICE
    )
    assert r.status_code == 400

    grants = (await client.get(f"/v1/datasets/{private['id']}/grants", headers=ALICE)).json()
    assert grants["can_manage"] is True
    owner_row = next(g for g in grants["data"] if g["permission"] == "OWNER")
    bob_row = next(g for g in grants["data"] if g["subject"]["id"] == "bob")
    # 至少保留一个 Owner（用户中心兜底）
    r = await client.delete(f"/v1/datasets/{private['id']}/grants/{owner_row['id']}", headers=ALICE)
    assert r.status_code == 409
    # 别的库的授权行 id 不能拿来用
    r = await client.delete(f"/v1/datasets/{team['id']}/grants/{bob_row['id']}", headers=ALICE)
    assert r.status_code == 404
    r = await client.delete(f"/v1/datasets/{private['id']}/grants/{bob_row['id']}", headers=ALICE)
    assert r.status_code == 204
    assert private["id"] not in await ids(BOB)

    # 「全部知识库 · Owner」（数据编码级授权，取代 kb:manage_all）：看全部、管全部
    rt.perms.type_levels["dave"] = "OWNER"
    dave = {"X-Uid": "dave"}
    assert await ids(dave) == {private["id"], shared["id"], team["id"]}
    assert (await client.get(f"/v1/datasets/{private['id']}", headers=dave)).json()["can_admin"]

    # 删除库时清掉用户中心里的授权
    r = await client.delete(f"/v1/datasets/{team['id']}", headers=ALICE)
    assert r.status_code == 204
    assert team["id"] not in rt.perms.rows


async def test_auth_uid_governs_dataset_permissions(client: Any, rt: Any) -> None:
    """库级权限一律按 X-Auth-Uid（内网身份传递 §6、§8）：建库人记 X-Uid，OWNER 是 auth-uid。"""
    from atlas_knowledge.access.auth import Principal
    from atlas_knowledge.dataset.datasets import DatasetService
    from atlas_knowledge.db.models import Dataset
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.errors import Forbidden
    from sqlalchemy import select

    on_behalf = {"X-Uid": "alice", "X-Auth-Uid": "de-1"}
    r = await client.post(
        "/v1/datasets",
        json={"name": "数字员工的库", "embedding_model": "fake-embedding"},
        headers=on_behalf,
    )
    assert r.status_code == 201, r.text
    ds = r.json()
    owner = next(g for g in rt.perms.rows[ds["id"]] if g["level"] == "OWNER")
    assert owner["subject_id"] == "de-1"
    async with get_sessionmaker()() as s:
        row = await s.scalar(select(Dataset).where(Dataset.uuid == UUID(ds["id"])))
        assert row is not None and row.created_by == "alice" and row.owner_id == "de-1"
    # alice 本人（auth-uid = alice）看不到；按 de-1 判的请求看得到
    assert ds["id"] not in {
        d["id"] for d in (await client.get("/v1/datasets", headers=ALICE)).json()["data"]
    }
    listed = (await client.get("/v1/datasets", headers=on_behalf)).json()["data"]
    assert ds["id"] in {d["id"] for d in listed}

    # 创建需要 kb:create
    bob = Principal(uid="bob", operations=frozenset())
    async with get_sessionmaker()() as s:
        with pytest.raises(Forbidden):
            await DatasetService(s, rt).create(bob, {"name": "x"})


async def test_agent_and_multi_retrieve(client: Any, rt: Any) -> None:
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
        "/v1/retrieve",
        json={
            "dataset_ids": [a["id"], b["id"]],
            "query": "请假要提前多久申请",
            "source": "agent",
            "source_ref": "thread-1",
            "retrieval_config": {"search_method": "semantic_search", "top_k": 2},
        },
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    recs = r.json()["records"]
    assert recs[0]["dataset_name"] == "B" and recs[0]["content"].startswith("请假")
    assert r.json()["skipped"] == []

    # 多库 + 加权重排（两库同一向量模型）
    r = await client.post(
        "/v1/retrieve",
        json={
            "dataset_ids": [a["id"], b["id"]],
            "query": "退款",
            "retrieval_config": {"reranking_enable": True, "top_k": 3},
        },
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    assert r.json()["records"][0]["content"].startswith("退款")

    r = await client.post("/v1/datasets/readable", json={}, headers=ALICE)
    assert {d["name"] for d in r.json()["data"]} == {"A", "B"}
    # 知识集方案 §3.2：knowledge_list 要列出文档数和最近入库时间
    assert all(d["document_count"] == 1 and d["updated_at"] for d in r.json()["data"])
    r = await client.post("/v1/datasets/readable", json={}, headers=BOB)
    assert r.json()["data"] == []


async def test_delete_dataset(client: Any, rt: Any) -> None:
    """删库发 DatasetDeleted：各领域删自己的数据（行、collection、原文件）；
    只查了这个库的检索记录一起删，跨库的留着。"""
    from atlas_knowledge.db import models as m
    from atlas_knowledge.db.session import get_sessionmaker
    from sqlalchemy import func, select

    ds = await _dataset(client)
    other = await _dataset(client, name="另一个库")
    await _upload(client, rt, ds["id"], process_rule=CUSTOM)
    await _upload(client, rt, other["id"], process_rule=CUSTOM)
    r = await client.post(
        f"/v1/datasets/{ds['id']}/metadata-fields", json={"name": "region"}, headers=ALICE
    )
    assert r.status_code == 201, r.text
    await _hit(client, ds["id"], "怎么申请退款")
    r = await client.post(
        "/v1/retrieve",
        json={"dataset_ids": [ds["id"], other["id"]], "query": "怎么申请退款"},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    collection = rt.vectors.for_collection(f"kb_{ds['id'].replace('-', '')}")
    assert await collection.point_ids()

    assert (await client.delete(f"/v1/datasets/{ds['id']}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/v1/datasets/{ds['id']}", headers=ALICE)).status_code == 404
    assert not any(k.startswith(f"{ds['id']}") or f"/{ds['id']}/" in k for k in rt.storage.objects)
    assert await collection.point_ids() == []
    gone = UUID(ds["id"])
    async with get_sessionmaker()() as s:
        for model in (
            m.Document,
            m.Segment,
            m.ChildChunk,
            m.ProcessRule,
            m.IndexVersion,
            m.QueryLog,
            m.MetadataField,
        ):
            n = await s.scalar(
                select(func.count()).select_from(model).where(model.dataset_uuid == gone)  # type: ignore[attr-defined]
            )
            assert n == 0, model.__tablename__
        records = [r.dataset_uuids for r in await s.scalars(select(m.Retrieval))]
    assert records == [[ds["id"], other["id"]]], "跨库的检索记录留着"


async def test_multi_retrieve_threshold_and_mixed_models(client: Any, rt: Any) -> None:
    """知识集方案 §2.1 / §5.2：门槛按每个库自己的分数、在合并前判断；向量模型不同时按名次合并。"""
    from conftest import FakeEmbedding

    class OtherEmbedding(FakeEmbedding):
        model = "fake-embedding-b"

    rt.registry.register_embedding(OtherEmbedding())
    a = await _dataset(client, name="A")
    b = await _dataset(client, name="B")
    c = await _dataset(client, name="C", embedding_model="fake-embedding-b")
    refund = "退款政策：七天内可以退款。\n\n发票自动开具。"
    leave = "请假需要提前一天申请。\n\n报销每周三处理。"
    await _upload(client, rt, a["id"], text=refund, process_rule=CUSTOM)
    for ds in (b, c):
        await _upload(client, rt, ds["id"], text=leave, process_rule=CUSTOM)

    async def ask(ids: list[str], **cfg: Any) -> list[dict[str, Any]]:
        r = await client.post(
            "/v1/retrieve",
            json={
                "dataset_ids": ids,
                "query": "请假要提前多久申请",
                "retrieval_config": {"search_method": "semantic_search", "top_k": 4, **cfg},
            },
            headers=ALICE,
        )
        assert r.status_code == 200, r.text
        return r.json()["records"]

    # 同一向量模型、不开门槛：直接比分数，A 的无关内容也会凑进前 4
    loose = await ask([a["id"], b["id"]])
    assert loose[0]["dataset_name"] == "B" and any(r["dataset_name"] == "A" for r in loose)
    # 开门槛：每个库先按自己的分数筛，A 的无关内容在合并前就被筛掉
    strict = await ask([a["id"], b["id"]], score_threshold_enabled=True, score_threshold=0.3)
    assert strict and {r["dataset_name"] for r in strict} == {"B"}
    # 向量模型不同：按名次合并 —— 各库的第一名先排（同分按库的顺序），而不是直接比分数
    mixed = await ask([a["id"], c["id"]])
    assert [r["dataset_name"] for r in mixed[:2]] == ["A", "C"]
    assert mixed[1]["content"].startswith("请假")


async def test_retrieve_by_knowledge_set_names(client: Any, rt: Any) -> None:
    """MCP 网关替 CLI 智能体调的就是这个检索接口：只给知识集和库的名称，在 auth-uid 能用的知识集里
    核对后换成库。身份照常取请求头（网关按身份凭据写好），本服务不判断请求从哪来。"""
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], process_rule=CUSTOM)
    gateway = {
        "X-Uid": "alice",
        "X-Auth-Uid": "alice",
        "X-Caller-App": "mcp-gateway",
        "X-Thread-Id": "thread-x",
    }
    # 不指定库就建的库：自带一个同名的知识集
    set_name = ds["name"]

    async def search(**body: Any) -> Any:
        return await client.post(
            "/v1/retrieve", json={"query": "怎么申请退款", "source": "mcp", **body}, headers=gateway
        )

    # 列库：按名称只要这一个知识集，带上其中能读的库的名片
    r = await client.post("/v1/knowledge-sets/readable", json={"name": set_name}, headers=gateway)
    assert r.status_code == 200, r.text
    [only] = r.json()["data"]
    assert [d["name"] for d in only["datasets"]] == [ds["name"]]
    assert only["datasets"][0]["document_count"] == 1

    r = await search(knowledge_set=set_name, knowledge_bases=[ds["name"]])
    assert r.status_code == 200, r.text
    assert r.json()["records"][0]["content"].startswith("退款政策")
    assert r.json()["records"][0]["dataset_name"] == ds["name"]
    # 只有一个能用的知识集：可以不填
    assert (await search()).json()["records"]
    # 填错了：说清楚可选的是什么（网关把这句话交给模型）
    r = await search(knowledge_set="人事")
    assert r.status_code == 400 and f"可选：{set_name}" in r.json()["detail"]
    r = await search(knowledge_set=set_name, knowledge_bases=["不存在"])
    assert r.status_code == 400 and "没有这些知识库：不存在" in r.json()["detail"]
    # 按库和按名称只能二选一
    r = await search(dataset_ids=[ds["id"]], knowledge_set=set_name)
    assert r.status_code == 400
    # 按 auth-uid 判：bob 一个能读的知识集都没有
    r = await client.post(
        "/v1/retrieve",
        json={"query": "退款", "knowledge_set": set_name},
        headers={**gateway, "X-Uid": "bob", "X-Auth-Uid": "bob"},
    )
    assert r.status_code == 400 and "没有能检索的知识集" in r.json()["detail"]
    # 缺 X-Uid 一律 401：没有请求体里的发起人这类兜底
    r = await client.post(
        "/v1/retrieve",
        json={"dataset_ids": [ds["id"]], "query": "退款", "actor": "alice"},
        headers={"X-Caller-App": "mcp-gateway"},
    )
    assert r.status_code == 401
    # 本服务不开 MCP 端点，也没有 /internal 了
    assert (await client.post("/mcp/", json={})).status_code == 404
    assert (await client.post("/internal/v1/retrieve", json={})).status_code == 404


RESUME_MD = """# 张三

## 项目经验

### 一、CI/CD 平台｜主导

- 组件版本治理：基于 Maven 聚合 pom 统一托管版本，灰度发布组件。
- 微环境：需求级 CI/CD 隔离，按需拉起依赖服务。

### 二、资信平台｜技术经理

- 资信数据按 uid 存储、按要素索引，业务方只凭 uid 取数。
- 对接多家资信机构，统一计费与缓存。

## 专业技能

| 方向 | 技能 |
| --- | --- |
| 后端 | Java、Spring Boot |
| 中间件 | Kafka、Redis |
"""


async def test_structure_chunking_end_to_end(client: Any, rt: Any) -> None:
    ds = await _dataset(client, doc_form="hierarchical_model")
    doc = await _upload(
        client, rt, ds["id"], text=RESUME_MD, name="resume.md"
    )  # 不传规则 → 默认按标题
    base = f"/v1/datasets/{ds['id']}/documents/{doc['id']}"
    assert (await client.get(base, headers=ALICE)).json()["process_rule"]["rules"][
        "parent_mode"
    ] == "structure"

    segs = (await client.get(f"{base}/segments", headers=ALICE)).json()["data"]
    assert [s["title_path"][-1] for s in segs] == [
        "一、CI/CD 平台｜主导",
        "二、资信平台｜技术经理",
        "专业技能",
    ]
    skills = segs[2]
    assert [c["content"] for c in skills["child_chunks"]] == [
        "| 后端 | Java、Spring Boot |",
        "| 中间件 | Kafka、Redis |",
    ]
    # 前缀 = 文档标题（上下文补全 L1）+ 标题路径 + 表头
    assert skills["child_chunks"][0]["context"] == "resume\n张三 > 专业技能\n| 方向 | 技能 |"

    # 子块原文里没有「资信平台」，靠前缀里的标题路径召回到正确的项目
    records = await _hit(client, ds["id"], "资信平台", search_method="full_text_search")
    assert records[0]["title_path"] == ["张三", "项目经验", "二、资信平台｜技术经理"]
    assert records[0]["content"].startswith("### 二、资信平台")
    assert not records[0]["child_chunks"][0]["content"].startswith("张三")  # 命中子块返回原文

    # 编辑父块：子块按结构重切，前缀仍是父块的标题路径
    seg = segs[1]
    r = await client.patch(
        f"{base}/segments/{seg['id']}",
        json={
            "content": "### 二、资信平台｜技术经理\n\n"
            "- 新增：反欺诈特征服务，实时出分。\n- 对接多家资信机构。"
        },
        headers=ALICE,
    )
    kids = r.json()["child_chunks"]
    # 第一条列表项不到 20 字，并进下一条：只有一个子块
    assert [k["content"] for k in kids] == [
        "- 新增：反欺诈特征服务，实时出分。\n- 对接多家资信机构。"
    ]
    assert all(k["context"] == "张三 > 项目经验 > 二、资信平台｜技术经理" for k in kids)
    records = await _hit(client, ds["id"], "反欺诈特征服务", search_method="full_text_search")
    assert records[0]["title_path"][-1] == "二、资信平台｜技术经理"
