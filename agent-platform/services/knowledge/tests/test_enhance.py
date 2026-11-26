"""分段增强（doc/knowledge-enterprise-design.html §5）：语义分段、上下文 L1 / L2、大表摘要入口。"""

from __future__ import annotations

import json
from typing import Any

from atlas_knowledge.parse.docir import Block, DocIR
from atlas_knowledge.segment import index_processor as ip
from atlas_knowledge.segment.enhance import (
    apply_llm_context,
    enhance,
    normalize_enhancements,
    tables_of,
)
from conftest import FakeEmbedding, run_indexing

ALICE = {"X-Uid": "alice"}
SUMMARY = "假期额度汇总：婚假最长，共十五天；事假最短"


class FakeChat:
    """按提示词类型回固定内容；记下调用次数（验证缓存）。"""

    model = "fake-llm"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def complete(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("llm down")
        prompt = messages[-1]["content"]
        if "表格" in prompt and "摘要" in prompt:
            return SUMMARY
        return "本段出自员工手册的假期章节"

    async def aclose(self) -> None:
        pass


def _rules(**structure: Any) -> dict[str, Any]:
    raw = {"parent_mode": "structure", "structure": structure}
    return ip.normalize_process_rule(
        "hierarchical", raw, doc_form="hierarchical_model", max_length=4000
    )[1]


# ───────────────────────────────────────────── 语义分段


def _two_topics() -> DocIR:
    a = "年假按工龄计算，年假需要提前申请，年假可以分次休完，年假当年有效。" * 2
    b = "差旅报销凭发票办理，报销单由部门经理签字，报销款每周三统一发放。" * 2
    return DocIR(
        blocks=[
            Block(type="heading", text="制度", level=1),
            *[Block(type="paragraph", text=f"{a}{i}") for i in range(3)],
            *[Block(type="paragraph", text=f"{b}{i}") for i in range(3)],
        ]
    )


def test_semantic_split_cuts_at_topic_change() -> None:
    embed = FakeEmbedding().embed_documents
    plain = ip.transform_ir(
        _two_topics(), doc_form="hierarchical_model", rules=_rules(max_length=300), embed=embed
    )
    assert any("年假" in n.page_content and "报销" in n.page_content for n in plain)

    semantic = ip.transform_ir(
        _two_topics(),
        doc_form="hierarchical_model",
        rules=_rules(max_length=300, semantic_split=True),
        embed=embed,
    )
    assert len(semantic) >= 2
    assert not any("年假" in n.page_content and "报销" in n.page_content for n in semantic)


# ───────────────────────────────────────────── 上下文 L1 / L2、大表摘要


def _leave_rows(n: int) -> list[list[str]]:
    kinds = ["年假", "病假", "婚假", "产假", "事假", "丧假"][:n]
    return [["假期", "天数"], *[[k, f"{i + 3} 天"] for i, k in enumerate(kinds)]]


def _leave_table(n: int) -> str:
    rows = _leave_rows(n)
    lines = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join([lines[0], "| --- | --- |", *lines[1:]])


def test_l1_context_has_doc_title_and_path() -> None:
    ir = DocIR(
        blocks=[
            Block(type="heading", text="第二章 假期", level=1),
            Block(type="paragraph", text="员工每年享有带薪年假。"),
        ],
        meta={"source": "员工手册.pdf"},
    )
    (node,) = ip.transform_ir(ir, doc_form="hierarchical_model", rules=_rules())
    assert node.metadata["context"].startswith("员工手册\n第二章 假期")
    assert all(c.metadata["context"].startswith("员工手册") for c in node.children)


async def test_enhance_contextual_and_table_summary() -> None:
    ir = DocIR(
        blocks=[
            Block(type="heading", text="假期", level=1),
            Block(type="paragraph", text="各类假期天数如下。"),
            Block(type="table", table={"rows": _leave_rows(5), "header_rows": 1}),
            # 行数不够阈值的表不做摘要
            Block(type="table", table={"rows": _leave_rows(2), "header_rows": 1}),
        ]
    )
    nodes = ip.transform_ir(ir, doc_form="hierarchical_model", rules=_rules(), doc_title="手册")
    llm = FakeChat()
    cfg = normalize_enhancements(
        {"contextual": True, "table_summary": True, "table_summary_min_rows": 4}
    )
    stats = await enhance(nodes, full_text=ir.plain_text(), tables=tables_of(ir), cfg=cfg, llm=llm)
    apply_llm_context(nodes)
    assert stats == {"contextual": 1, "table_summaries": 1, "failed": 0}
    node = nodes[0]
    assert node.metadata["context"].endswith("本段出自员工手册的假期章节")
    summary = [c for c in node.children if c.metadata.get("type") == "summary"]
    assert len(summary) == 1 and summary[0].page_content == SUMMARY
    # 摘要子块也拼上了 L2 上下文；普通子块同样
    assert all("本段出自" in c.metadata["context"] for c in node.children)


async def test_enhance_degrades_without_or_with_failing_llm() -> None:
    ir = DocIR(blocks=[Block(type="heading", text="A", level=1), Block(type="paragraph", text="x")])
    nodes = ip.transform_ir(ir, doc_form="hierarchical_model", rules=_rules())
    cfg = normalize_enhancements({"contextual": True})
    assert (await enhance(nodes, full_text="x", cfg=cfg, llm=None))["contextual"] == 0
    stats = await enhance(nodes, full_text="x", cfg=cfg, llm=FakeChat(fail=True))
    assert stats["failed"] == 1 and "llm_context" not in nodes[0].metadata


# ───────────────────────────────────────────── 增强结果缓存（知识库的库里，带租户）


async def test_enhance_results_are_cached_in_the_database_per_tenant(rt: Any) -> None:
    """重跑入库不用再调模型：结果存在 kb_enhance_cache（带租户），不在共用的 Redis 里。
    别的租户上传同一份内容不命中；超过保留期的被清掉；删除租户时一起删。"""
    from datetime import timedelta
    from uuid import UUID

    from atlas_knowledge.db.models import EnhanceCacheEntry
    from atlas_knowledge.db.session import get_sessionmaker
    from atlas_knowledge.db.types import utcnow
    from atlas_knowledge.ops.lifecycle import PURGE_ORDER
    from atlas_knowledge.segment.enhance_cache import RETENTION, DbEnhanceCache, cleanup
    from atlas_tenancy import SystemReason, TenantDataPurge, TenantScope
    from conftest import OTHER_TENANT, TEST_TENANT
    from sqlalchemy import func, select, update

    ir = DocIR(
        blocks=[
            Block(type="heading", text="假期", level=1),
            Block(type="paragraph", text="各类假期天数如下。"),
            Block(type="table", table={"rows": _leave_rows(5), "header_rows": 1}),
        ]
    )
    cfg = normalize_enhancements(
        {"contextual": True, "table_summary": True, "table_summary_min_rows": 4}
    )
    cache = DbEnhanceCache(get_sessionmaker())

    async def run() -> tuple[FakeChat, dict[str, int]]:
        nodes = ip.transform_ir(ir, doc_form="hierarchical_model", rules=_rules(), doc_title="手册")
        llm = FakeChat()
        stats = await enhance(
            nodes, full_text=ir.plain_text(), tables=tables_of(ir), cfg=cfg, llm=llm, cache=cache
        )
        return llm, stats

    first, stats = await run()
    assert first.calls == 2 and stats == {"contextual": 1, "table_summaries": 1, "failed": 0}
    again, stats = await run()
    assert again.calls == 0, "重跑：全部命中，不再调模型"
    assert stats == {"contextual": 1, "table_summaries": 1, "failed": 0}
    with TenantScope.enter(UUID(OTHER_TENANT)):
        theirs, _ = await run()
    assert theirs.calls == 2, "别的租户同一份内容不命中"

    # 超过保留期：查不到，也被每天的清理删掉
    async with get_sessionmaker()() as s:
        with TenantScope.system(SystemReason.SWEEP):
            await s.execute(
                update(EnhanceCacheEntry)
                .where(EnhanceCacheEntry.tenant_uuid == UUID(OTHER_TENANT))
                .values(created_at=utcnow() - RETENTION - timedelta(days=1))
            )
        await s.commit()
    with TenantScope.enter(UUID(OTHER_TENANT)):
        stale, _ = await run()
    assert stale.calls == 2, "过期的不算命中"
    with TenantScope.isolated():
        assert await cleanup(get_sessionmaker()) == 2

    # 删除租户：一起删（在删除顺序里）
    async with get_sessionmaker()() as s:
        with TenantScope.system(SystemReason.OPS):
            await TenantDataPurge(PURGE_ORDER).batch(s, UUID(TEST_TENANT))
            await s.commit()
        with TenantScope.system(SystemReason.SWEEP):
            left = await s.scalar(
                select(func.count())
                .select_from(EnhanceCacheEntry)
                .where(EnhanceCacheEntry.tenant_uuid == UUID(TEST_TENANT))
            )
    assert left == 0


# ───────────────────────────────────────────── 接口：开关 → 入库 → 摘要入口被检索命中


async def test_dataset_enhancements_end_to_end(client: Any, rt: Any) -> None:
    rt.llm = FakeChat()
    r = await client.post(
        "/v1/datasets",
        json={
            "name": "手册",
            "embedding_model": "fake-embedding",
            "enhancements": {
                "contextual": True,
                "table_summary": True,
                "table_summary_min_rows": 4,
            },
        },
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    ds = r.json()
    assert ds["enhancements"]["table_summary"] is True

    text = "# 假期\n\n各类假期的天数见下表。\n\n" + _leave_table(6) + "\n"
    r = await client.post(
        f"/v1/datasets/{ds['id']}/documents",
        files=[("files", ("员工手册.md", text.encode(), "text/markdown"))],
        data={"process_rule": json.dumps({"mode": "automatic"})},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    doc = r.json()["documents"][0]
    await run_indexing(rt)

    base = f"/v1/datasets/{ds['id']}/documents/{doc['id']}"
    info = (await client.get(base, headers=ALICE)).json()
    assert info["indexing_status"] == "completed", info
    assert info["parse_report"]["enhance"]["table_summaries"] == 1
    segs = (await client.get(f"{base}/segments", headers=ALICE)).json()["data"]
    assert segs and all("本段出自员工手册的假期章节" in (s["context"] or "") for s in segs)

    r = await client.post(
        "/v1/retrieve",
        json={
            "dataset_ids": [ds["id"]],
            "query": SUMMARY,
            "retrieval_config": {"search_method": "semantic_search", "top_k": 1},
        },
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    (rec,) = r.json()["records"]
    # 汇总问题由摘要入口命中，回到挂摘要的分段（自动分段按标题切：整张表和标题在同一段）
    assert [(c["kind"], c["content"]) for c in rec["child_chunks"]] == [("summary", SUMMARY)]
    assert rec["content"].startswith("# 假期") and "| 丧假 | 8 天 |" in rec["content"]

    # 关掉开关只影响之后入库的文档
    r = await client.patch(
        f"/v1/datasets/{ds['id']}", json={"enhancements": {"contextual": False}}, headers=ALICE
    )
    assert r.status_code == 200, r.text
    assert r.json()["enhancements"] == normalize_enhancements({"contextual": False})
