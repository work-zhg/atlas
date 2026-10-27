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
    """库级权限一律按 X-Auth-Uid（内网身份传递 §6、§8）：建库人记 X-Uid，OWNER 是 auth-uid。

    存量服务账号的库（owner_kind = service，没登记到用户中心）只有「全部知识库」授权的人看得到、
    不能共享；sync-uc --service-owner 把它改归一个人。
    """
    from atlas_knowledge.auth import Principal
    from atlas_knowledge.db.models import Dataset
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.errors import Forbidden
    from atlas_knowledge.services.datasets import DatasetService
    from atlas_knowledge.sync_uc import sync_uc
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

    # 存量服务账号的库
    legacy = Dataset(
        name="评测库",
        owner_id="sa-bench",
        owner_kind="service",
        indexing_technique="high_quality",
        embedding_model="fake-embedding",
        embedding_dim=8,
        retrieval_config={},
        collection_name="kb_legacy",
        created_by="sa-bench",
    )
    async with get_sessionmaker()() as s:
        s.add(legacy)
        await s.commit()
    r = await client.get("/v1/datasets", headers=ALICE)
    assert str(legacy.uuid) not in {d["id"] for d in r.json()["data"]}
    rt.perms.type_levels["alice"] = "READ"
    r = await client.get(f"/v1/datasets/{legacy.uuid}", headers=ALICE)
    assert r.status_code == 200 and r.json()["my_level"] == "READ"
    rt.perms.type_levels.pop("alice")
    async with get_sessionmaker()() as s:
        out = await sync_uc(s, rt.perms, service_owner="bench-user")
        assert out["adopted"] == 1
        await s.refresh(await s.merge(legacy))
    bench = {"X-Uid": "bench-user"}
    r = await client.get(f"/v1/datasets/{legacy.uuid}", headers=bench)
    assert r.status_code == 200 and r.json()["can_admin"] and r.json()["owner_kind"] == "user"

    # 创建需要 kb:create
    bob = Principal(uid="bob", operations=frozenset())
    async with get_sessionmaker()() as s:
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

    r = await client.post("/internal/v1/datasets/readable", json={}, headers=ALICE)
    assert {d["name"] for d in r.json()["data"]} == {"A", "B"}
    r = await client.post("/internal/v1/datasets/readable", json={}, headers=BOB)
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
    rt.settings = rt.settings.model_copy(update={"mcp_secret": SecretStr("s3cret")})

    app = create_app(runtime=rt)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    async def call(c: Any, token: str, header: str = "X-Knowledge-Token") -> str:
        h = {**headers, header: token}
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
            # 旧名（带应用名）过渡一个版本仍然认：已打开的会话里 MCP 配置还是它（数字员工设计 §7.4）
            legacy = await call(c, _sign("s3cret", [ds["id"]]), "X-Atlas-Knowledge-Token")
            assert "退款政策" in legacy


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


async def test_sync_uc_moves_legacy_visibility_into_the_user_center(rt: Any) -> None:
    """一次性迁移：旧的可见范围 / 成员 → 用户中心；所有者不在用户中心的认定为服务账号的库。"""
    from uuid import uuid4

    from atlas_knowledge.db.models import Dataset, DatasetPermission
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.errors import Invalid
    from atlas_knowledge.sync_uc import sync_uc
    from conftest import FakeDataPermissions

    class Perms(FakeDataPermissions):
        async def create(self, dataset_id: str, name: str, grants: list[Any]) -> None:
            if grants[0].subject_id == "sa-old":
                raise Invalid("用户不存在")
            await super().create(dataset_id, name, grants)

    def row(owner: str, permission: str) -> Dataset:
        u = uuid4()
        return Dataset(
            uuid=u,
            name=f"{owner}-{permission}",
            owner_id=owner,
            permission=permission,
            indexing_technique="high_quality",
            embedding_model="fake-embedding",
            embedding_dim=8,
            retrieval_config={},
            collection_name=f"kb_{u.hex}",
            created_by=owner,
        )

    perms = Perms()
    team, partial, mine, sa = (
        row("alice", "all_team"),
        row("alice", "partial_members"),
        row("alice", "only_me"),
        row("sa-old", "only_me"),
    )
    async with get_sessionmaker()() as s:
        s.add_all([team, partial, mine, sa])
        await s.flush()
        s.add(DatasetPermission(dataset_uuid=partial.uuid, principal_id="bob", level="write"))
        await s.commit()
        out = await sync_uc(s, perms)
        assert out == {"synced": 3, "skipped": 0, "service": 1, "member_failed": 0, "adopted": 0}
        assert await perms.level("bob", str(team.uuid)) == "READ"  # 全员只读
        assert await perms.level("bob", str(partial.uuid)) == "WRITE"
        assert await perms.level("bob", str(mine.uuid)) == "NONE"
        await s.refresh(sa)
        assert sa.owner_kind == "service"
        # 可以重复执行
        assert (await sync_uc(s, perms))["skipped"] == 3
