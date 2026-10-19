"""检索（doc/knowledge-enterprise-design.html §7）：查询理解、元数据过滤、文档级权限、
版本与时效、RRF、结果多样性、漏斗。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from atlas_knowledge.errors import Invalid
from atlas_knowledge.rag.filters import BUILTIN_FIELDS, FieldDef, compile_filter, validate_filter
from atlas_knowledge.rag.retrieval import rrf
from conftest import run_indexing

ALICE = {"X-Uid": "alice"}
CUSTOM = {
    "mode": "custom",
    "rules": {"segmentation": {"separator": "\\n\\n", "max_tokens": 200, "chunk_overlap": 0}},
}
SEMANTIC = {"search_method": "semantic_search", "top_k": 5}


class ScriptedChat:
    """查询理解的假 LLM：按脚本回 JSON，记下提示词。"""

    model = "fake-llm"

    def __init__(self, reply: dict[str, Any] | Exception) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.prompts.append(messages[-1]["content"])
        if isinstance(self.reply, Exception):
            raise self.reply
        return json.dumps(self.reply, ensure_ascii=False)


async def _dataset(client: Any, **body: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/datasets",
        json={"name": "制度库", "embedding_model": "fake-embedding", **body},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _upload(
    client: Any, rt: Any, ds_id: str, text: str, name: str, **form: Any
) -> dict[str, Any]:
    data = {"process_rule": json.dumps(CUSTOM)}
    data.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in form.items()})
    r = await client.post(
        f"/v1/datasets/{ds_id}/documents",
        files=[("files", (name, text.encode(), "text/plain"))],
        data=data,
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    await run_indexing(rt)
    return r.json()["documents"][0]


async def _hit(client: Any, ds_id: str, query: str, **body: Any) -> dict[str, Any]:
    r = await client.post(
        f"/v1/datasets/{ds_id}/hit-testing",
        json={"query": query, "retrieval_config": body.pop("cfg", SEMANTIC), **body},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _docs(res: dict[str, Any]) -> set[str]:
    return {r["document_name"] for r in res["records"]}


# ───────────────────────────────────────────── 过滤表达式


def test_filter_validation_and_compile() -> None:
    fields = {**BUILTIN_FIELDS, "year": FieldDef("year", "number")}
    assert validate_filter({"field": "year", "op": "gte", "value": "2026"}, fields) == {
        "field": "year",
        "op": "range",
        "value": {"gte": 2026.0},
    }
    for bad in (
        {"field": "nope", "op": "eq", "value": 1},
        {"field": "doc_type", "op": "gt", "value": 1},
        {"field": "year", "op": "like", "value": 1},
        {"or": []},
    ):
        with pytest.raises(Invalid):
            validate_filter(bad, fields)
    # 多库：某库没定义的字段在该库视为不限（and 里去掉；or 里整个 or 不限）
    expr = validate_filter(
        {"or": [{"field": "year", "op": "eq", "value": 1}, {"field": "doc_type", "value": "pdf"}]},
        fields,
    )
    assert compile_filter(expr, dict(BUILTIN_FIELDS)) is None
    assert compile_filter(expr, fields) is not None


def test_rrf_rewards_items_ranked_high_in_many_lists() -> None:
    out = rrf([["a", "b", "c"], ["b", "c", "a"], ["b", "d"]], key=lambda x: x)
    assert out[0] == "b" and set(out) == {"a", "b", "c", "d"}


# ───────────────────────────────────────────── 文档级权限


async def test_document_acl_enforced_for_agent_calls(client: Any, rt: Any) -> None:
    tags = {"bob": ["u:bob", "d:sales", "d:company"], "carol": ["u:carol", "d:hr"]}

    async def resolver(user_id: str) -> list[str]:
        return tags.get(user_id, [f"u:{user_id}"])

    rt.principal_resolver = resolver
    ds = await _dataset(client)
    await _upload(client, rt, ds["id"], "薪酬调整方案：人力资源部内部。", "hr.txt", acl=["d:hr"])
    await _upload(client, rt, ds["id"], "差旅报销制度：全员适用。", "public.txt")
    sales = await _upload(
        client, rt, ds["id"], "销售提成方案：按季度结算。", "sales.txt", acl=["d:company"]
    )

    async def internal(actor: str | None) -> set[str]:
        # 内网免令牌：atlas-server 带这一轮的身份请求头；文档级权限按 X-Auth-Uid
        r = await client.post(
            "/internal/v1/retrieve",
            json={"dataset_ids": [ds["id"]], "query": "方案", "retrieval_config": SEMANTIC},
            headers={"X-Uid": "zhang", "X-Auth-Uid": actor} if actor else {},
        )
        assert r.status_code == 200, r.text
        return {x["document_name"] for x in r.json()["records"]}

    # 按 auth-uid：bob 在销售部（上级部门 company 的授权也算）；carol 在人力；都没有只看公开
    assert await internal("bob") == {"public.txt", "sales.txt"}
    assert await internal("carol") == {"public.txt", "hr.txt"}
    assert await internal(None) == {"public.txt"}
    # 库管理员调试（hit-testing）不受文档级权限限制
    assert _docs(await _hit(client, ds["id"], "方案")) == {"hr.txt", "public.txt", "sales.txt"}

    # 权限变更即时生效（只改 payload）
    r = await client.patch(
        f"/v1/datasets/{ds['id']}/documents/{sales['id']}", json={"acl": ["u:carol"]}, headers=ALICE
    )
    assert r.status_code == 200 and r.json()["acl"] == ["u:carol"]
    assert await internal("bob") == {"public.txt"}
    bad = await client.patch(
        f"/v1/datasets/{ds['id']}/documents/{sales['id']}", json={"acl": ["bob"]}, headers=ALICE
    )
    assert bad.status_code == 400


# ───────────────────────────────────────────── 元数据过滤 + 查询理解抽取


async def test_metadata_filter_and_llm_extraction(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    base = f"/v1/datasets/{ds['id']}"
    r = await client.post(
        f"{base}/metadata-fields",
        json={"name": "year", "type": "number", "description": "制度版本年份", "llm_extract": True},
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    await client.post(
        f"{base}/metadata-fields",
        json={"name": "region", "type": "enum", "options": ["华东", "华北"]},
        headers=ALICE,
    )
    fields = (await client.get(f"{base}/metadata-fields", headers=ALICE)).json()["data"]
    assert {f["name"] for f in fields} >= {"year", "region", "doc_type", "uploaded_at"}

    await _upload(
        client, rt, ds["id"], "差旅标准：每晚三百元。", "2025.txt", metadata={"year": 2025}
    )
    await _upload(
        client,
        rt,
        ds["id"],
        "差旅标准：每晚五百元。",
        "2026.txt",
        metadata={"year": 2026, "region": "华东"},
    )
    bad = await client.post(
        f"{base}/documents",
        files=[("files", ("x.txt", b"x", "text/plain"))],
        data={"metadata": json.dumps({"region": "西南"})},
        headers=ALICE,
    )
    assert bad.status_code == 400

    flt = {"field": "year", "op": "gte", "value": 2026}
    assert _docs(await _hit(client, ds["id"], "差旅标准", filter=flt)) == {"2026.txt"}
    both = {"and": [flt, {"field": "region", "op": "in", "value": ["华北"]}]}
    assert _docs(await _hit(client, ds["id"], "差旅标准", filter=both)) == set()
    assert _docs(
        await _hit(client, ds["id"], "差旅标准", filter={"field": "doc_type", "value": "txt"})
    )
    r = await client.post(
        f"{base}/hit-testing",
        json={"query": "x", "filter": {"field": "nope", "value": 1}},
        headers=ALICE,
    )
    assert r.status_code == 400

    # 问题里说了「2025 版」：查询理解抽出 year = 2025，只收窄
    rt.llm = ScriptedChat({"filters": [{"field": "year", "op": "eq", "value": 2025}]})
    res = await _hit(client, ds["id"], "2025 版的差旅标准")
    assert _docs(res) == {"2025.txt"}
    assert res["query_plan"]["filters"] == {"field": "year", "op": "eq", "value": 2025.0}
    assert "year（number）" in rt.llm.prompts[0]
    # 抽出没定义 / 不允许抽取的字段：丢掉，不报错
    rt.llm = ScriptedChat({"filters": [{"field": "region", "op": "eq", "value": "华东"}]})
    assert _docs(await _hit(client, ds["id"], "华东 2025 版的差旅标准")) == {"2025.txt", "2026.txt"}


# ───────────────────────────────────────────── 版本与时效


async def test_new_version_supersedes_old(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    v1 = await _upload(client, rt, ds["id"], "年假：每年五天。", "年假制度-v1.txt")
    before = datetime.now(UTC)
    v2 = await _upload(
        client, rt, ds["id"], "年假：每年十天。", "年假制度-v2.txt", supersedes=v1["id"]
    )
    base = f"/v1/datasets/{ds['id']}/documents"
    old = (await client.get(f"{base}/{v1['id']}", headers=ALICE)).json()
    new = (await client.get(f"{base}/{v2['id']}", headers=ALICE)).json()
    assert old["is_current"] is False and old["valid_to"] is not None
    assert new["is_current"] is True and new["version_group"] == old["version_group"]

    assert _docs(await _hit(client, ds["id"], "年假几天")) == {"年假制度-v2.txt"}
    res = await _hit(client, ds["id"], "年假几天", include_superseded=True)
    assert _docs(res) == {"年假制度-v1.txt", "年假制度-v2.txt"}
    stale = next(r for r in res["records"] if r["document_name"] == "年假制度-v1.txt")
    assert stale["document_version"]["is_current"] is False
    # 按切换之前的时间点检索：只有旧版本
    past = (before - timedelta(seconds=1)).isoformat()
    assert _docs(await _hit(client, ds["id"], "年假几天", as_of=past)) == {"年假制度-v1.txt"}


async def test_validity_window(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await _upload(client, rt, ds["id"], "新规：下月起报销线上提交。", "new.txt", valid_from=future)
    await _upload(client, rt, ds["id"], "旧规：报销纸质提交。", "old.txt", valid_to=expired)
    await _upload(client, rt, ds["id"], "通用：报销需发票。", "now.txt")
    assert _docs(await _hit(client, ds["id"], "报销")) == {"now.txt"}
    later = (datetime.now(UTC) + timedelta(days=31)).isoformat()
    assert _docs(await _hit(client, ds["id"], "报销", as_of=later)) == {"new.txt", "now.txt"}


# ───────────────────────────────────────────── 查询理解：改写、多查询、HyDE、分解、降级


async def test_rewrite_with_history_and_multi_query(client: Any, rt: Any) -> None:
    ds = await _dataset(client, retrieval_config={**SEMANTIC, "multi_query": True, "hyde": True})
    await _upload(client, rt, ds["id"], "出差住宿标准：一线城市每晚五百元。", "hotel.txt")
    await _upload(client, rt, ds["id"], "出差餐补：每天一百元。", "meal.txt")
    rt.llm = ScriptedChat(
        {
            "rewrite": "出差住宿标准是多少",
            "queries": ["差旅酒店报销上限", "出差住宿费用标准"],
            "hyde": "一线城市住宿每晚不超过五百元。",
        }
    )
    history = [
        {"role": "user", "content": "出差有哪些补贴？"},
        {"role": "assistant", "content": "有餐补和住宿补贴。"},
    ]
    res = await _hit(client, ds["id"], "那住宿呢", history=history, cfg=None)
    assert res["records"][0]["document_name"] == "hotel.txt"
    plan = res["query_plan"]
    assert plan["rewrite"] == "出差住宿标准是多少" and len(plan["queries"]) == 2
    assert "对话历史" in rt.llm.prompts[0]

    # 漏斗：查询理解 → 各表述召回 → RRF
    funnel = (await client.get(f"/v1/retrievals/{res['retrieval_id']}", headers=ALICE)).json()
    names = [s["name"] for s in funnel["stages"]]
    assert names[0] == "query_understanding"
    assert names.count("semantic") == 4  # 改写 + 2 个表述 + HyDE
    assert "merged" in names

    # 同一问题再来：走缓存，不再调 LLM
    calls = len(rt.llm.prompts)
    await _hit(client, ds["id"], "那住宿呢", history=history, cfg=None)
    assert len(rt.llm.prompts) == calls


async def test_decomposition_groups_by_sub_question(client: Any, rt: Any) -> None:
    ds = await _dataset(client, retrieval_config={**SEMANTIC, "decompose": True})
    await _upload(client, rt, ds["id"], "北京分公司年假十天。", "bj.txt")
    await _upload(client, rt, ds["id"], "上海分公司年假十二天。", "sh.txt")
    await _upload(client, rt, ds["id"], "食堂中午十二点开饭。", "canteen.txt")
    rt.llm = ScriptedChat({"sub_questions": ["北京分公司年假几天", "上海分公司年假几天"]})
    res = await _hit(client, ds["id"], "北京和上海分公司的年假差几天", cfg=None)
    assert res["sub_questions"] == ["北京分公司年假几天", "上海分公司年假几天"]
    top2 = {r["document_name"]: r["sub_question"] for r in res["records"][:2]}
    assert top2 == {"bj.txt": 0, "sh.txt": 1}


async def test_query_understanding_failure_falls_back(client: Any, rt: Any) -> None:
    ds = await _dataset(client, retrieval_config={**SEMANTIC, "multi_query": True})
    await _upload(client, rt, ds["id"], "报销每周三处理。", "a.txt")
    rt.llm = ScriptedChat(RuntimeError("llm down"))
    res = await _hit(client, ds["id"], "报销什么时候处理", cfg=None)
    assert res["records"] and "error" in res["query_plan"]


# ───────────────────────────────────────────── 结果多样性


async def test_per_document_cap_and_adjacent_merge(client: Any, rt: Any) -> None:
    ds = await _dataset(client)
    text = "报销规则一：发票。\n\n报销规则二：审批。\n\n报销规则三：打款。\n\n无关内容：食堂。"
    await _upload(client, rt, ds["id"], text, "rules.txt")
    await _upload(client, rt, ds["id"], "报销问答：常见问题。", "faq.txt")

    capped = await _hit(client, ds["id"], "报销规则", cfg={**SEMANTIC, "max_per_document": 1})
    assert sorted(r["document_name"] for r in capped["records"]) == ["faq.txt", "rules.txt"]

    merged = await _hit(
        client, ds["id"], "报销规则", cfg={**SEMANTIC, "top_k": 3, "merge_adjacent": True}
    )
    rules = [r for r in merged["records"] if r["document_name"] == "rules.txt"]
    assert len(rules) == 1 and len(rules[0]["merged_segments"]) >= 1
    assert "规则一" in rules[0]["content"] and "规则二" in rules[0]["content"]

    mmr = await _hit(client, ds["id"], "报销规则", cfg={**SEMANTIC, "mmr_enable": True})
    assert len(mmr["records"]) == len({r["segment_uuid"] for r in mmr["records"]})
