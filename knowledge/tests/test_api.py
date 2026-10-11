"""接口全流程：建库 → 上传 → 入库 → 召回测试 → 分段管理 → 状态 / 审核 → 权限。"""

from __future__ import annotations

import json
from typing import Any

from conftest import run_indexing

ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}

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
    assert refund["hit_count"] >= 1
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


async def test_review_required(client: Any, rt: Any) -> None:
    ds = await _dataset(client, review_required=True, permission="all_team")
    doc = await _upload(client, rt, ds["id"], process_rule=CUSTOM)
    assert doc["review_status"] == "pending"
    assert await _hit(client, ds["id"], "退款", search_method="full_text_search") == []

    # bob 能读（all_team），但没有 kb:review —— dev 模式操作码全有，所以用 owner 审核
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents-review",
        json={"document_ids": [doc["id"]], "approve": True, "note": "已核对"},
        headers=ALICE,
    )
    assert r.status_code == 200
    assert r.json()["documents"][0]["review_status"] == "approved"
    assert await _hit(client, ds["id"], "退款", search_method="full_text_search")


async def test_permissions(client: Any, rt: Any) -> None:
    from atlas_knowledge.auth import Principal
    from atlas_knowledge.services.permission import DatasetAccess

    private = await _dataset(client)
    # dev 模式下 bob 也有 kb:manage_all —— 用判定类直接验证规则
    ds_row: Any = type("D", (), {"owner_id": "alice", "permission": "only_me"})()
    bob = Principal(id="bob", operations=frozenset())
    assert not DatasetAccess(ds_row, bob, None).can_read
    ds_row.permission = "all_team"
    acc = DatasetAccess(ds_row, bob, None)
    assert acc.can_read and not acc.can_write
    ds_row.permission = "partial_members"
    assert not DatasetAccess(ds_row, bob, None).can_read
    assert DatasetAccess(ds_row, bob, "read").can_read
    assert DatasetAccess(ds_row, bob, "write").can_write
    assert not DatasetAccess(ds_row, bob, "write").is_admin
    assert DatasetAccess(
        ds_row, Principal(id="x", operations=frozenset({"kb:manage_all"})), None
    ).is_admin

    # 列表的可见性查询
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.services.datasets import DatasetService

    shared = await _dataset(
        client, permission="partial_members", members=[{"id": "bob", "level": "read"}]
    )
    team = await _dataset(client, permission="all_team")
    async with get_sessionmaker()() as s:
        rows, total = await DatasetService(s, rt).list(bob)
    ids = {str(r.uuid) for r in rows}
    assert ids == {shared["id"], team["id"]} and total == 2 and private["id"] not in ids

    # 创建需要 kb:create
    async with get_sessionmaker()() as s:
        import pytest
        from atlas_knowledge.errors import Forbidden

        with pytest.raises(Forbidden):
            await DatasetService(s, rt).create(bob, {"name": "x"})


async def test_internal_and_multi_retrieve(client: Any, rt: Any) -> None:
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
            "query": "请假要提前多久申请",
            "source": "agent",
            "source_ref": "thread-1",
            "retrieval_config": {"search_method": "semantic_search", "top_k": 2},
        },
    )
    assert r.status_code == 200, r.text
    recs = r.json()["records"]
    assert recs[0]["dataset_name"] == "B" and recs[0]["content"].startswith("请假")

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

    r = await client.post("/internal/v1/datasets/readable", json={"principal_id": "alice"})
    assert {d["name"] for d in r.json()["data"]} == {"A", "B"}
    r = await client.post("/internal/v1/datasets/readable", json={"principal_id": "bob"})
    assert r.json()["data"] == []


async def test_delete_dataset(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], process_rule=CUSTOM)
    assert (await client.delete(f"/v1/datasets/{ds['id']}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/v1/datasets/{ds['id']}", headers=ALICE)).status_code == 404
    assert rt.storage.objects == {}


def _sign(secret: str, dataset_ids: list[str]) -> str:
    """与 atlas-server providers/knowledge.py 的 sign_mcp_token 同一算法。"""
    import base64
    import hashlib
    import hmac

    payload = json.dumps(
        {"d": dataset_ids, "t": "thread-x", "u": None}, separators=(",", ":"), sort_keys=True
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()}"


async def test_mcp_knowledge_search(client: Any, rt: Any) -> None:
    import httpx
    from atlas_knowledge.api.app import create_app
    from pydantic import SecretStr

    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], process_rule=CUSTOM)
    rt.settings = rt.settings.model_copy(update={"internal_token": SecretStr("s3cret")})

    app = create_app(runtime=rt)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    async def call(c: Any, token: str) -> str:
        h = {**headers, "X-Atlas-Knowledge-Token": token}
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        r = await c.post("/mcp/", json=init, headers=h)
        assert r.status_code == 200, r.text
        listed = await c.post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h
        )
        assert [t["name"] for t in listed.json()["result"]["tools"]] == ["knowledge_search"]
        r = await c.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "knowledge_search",
                    "arguments": {"query": "怎么申请退款"},
                },
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        return r.json()["result"]["content"][0]["text"]

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kb") as c:
            text = await call(c, _sign("s3cret", [ds["id"]]))
            assert "退款政策" in text and "faq.txt" in text
            assert "凭据无效" in await call(c, _sign("wrong", [ds["id"]]))
