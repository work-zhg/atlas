"""查询理解（doc/knowledge-enterprise-design.html §7.1）：检索前的阶段 ⓪。

一次结构化输出的 LLM 调用，按需产出：
    rewrite        结合对话历史改写成独立完整的问题（有历史、且问题含指代或过短时）
    queries        2–3 个不同表述（多查询，按库开关）
    hyde           假设性答案，用它做语义召回（按库开关）
    sub_questions  复合问题拆成的子问题（按库开关，由模型判定是否复合）
    filters        从问题里抽取的元数据过滤条件（库里定义了允许抽取的字段时）
    datasets       多库路由：选出最相关的库（绑定的库超过阈值时）

★ 只问需要的任务；一个都不需要（或没配 LLM）就不调用。
★ 结果按「问题 + 历史 + 任务 + 字段 + 库」哈希缓存在 Redis。
★ 降级：调用失败、超时、输出不合法 → 原问题检索，不让检索失败。
★ 抽取的过滤条件只能收窄（与调用方的过滤取与），且只作用于业务元数据；权限、版本由服务端附加。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import Invalid
from .filters import FieldDef, validate_filter
from .llm import ChatModel, parse_json
from .models import text_hash

__all__ = ["QueryPlan", "understand"]

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 3600
_TIMEOUT_S = 20.0
#: 指代词 / 省略：有对话历史时触发改写
_REFERENCE = re.compile(r"[它他她其该此这那]|上述|上面|前面|刚才|那个|这个|呢[？?]?$")
_SHORT = 8
_HISTORY_TURNS = 6
_HISTORY_CHARS = 600


@dataclass
class QueryPlan:
    original: str
    #: 检索与重排用的主问题（改写后）
    query: str
    #: 召回用的各个表述：(文本, 检索方式覆盖)；检索方式 None = 库的配置
    variants: list[tuple[str, str | None]] = field(default_factory=list)
    sub_questions: list[str] = field(default_factory=list)
    #: 抽取的过滤条件（已校验）
    extracted: dict[str, Any] | None = None
    #: 路由选中的库（uuid）；None = 全部
    datasets: list[str] | None = None
    #: 写进漏斗的说明
    notes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def plain(cls, query: str, **notes: Any) -> QueryPlan:
        return cls(original=query, query=query, variants=[(query, None)], notes=dict(notes))


_PROMPT = """你是企业知识库的检索助手。根据用户的问题，完成下列任务，只输出一个 JSON 对象。

任务：
{tasks}

{context}
用户问题：{query}

输出 JSON 的键（只输出上面要求的键）：
{keys}"""


def _tasks(
    *,
    rewrite: bool,
    multi: bool,
    hyde: bool,
    decompose: bool,
    fields: list[FieldDef],
    datasets: list[dict[str, str]],
) -> tuple[list[str], list[str]]:
    tasks: list[str] = []
    keys: list[str] = []
    if rewrite:
        tasks.append("结合对话历史，把用户问题改写成一个不依赖上下文、独立完整的问题。")
        keys.append('"rewrite": 改写后的问题')
    if multi:
        tasks.append("再给出 2 个意思相同、表述不同的检索问题（换用同义词、口语 / 书面语）。")
        keys.append('"queries": [问题, 问题]')
    if hyde:
        tasks.append("写一段 100 字以内的假设性答案（像知识库原文那样陈述，不确定的细节可以编）。")
        keys.append('"hyde": 假设性答案')
    if decompose:
        tasks.append(
            "判断问题是否是复合问题（对比、多个条件、多跳）。"
            "是的话拆成 2–4 个可以分别检索的子问题；不是就给空数组。"
        )
        keys.append('"sub_questions": [子问题, ...]')
    if fields:
        desc = "\n".join(
            f"  - {f.name}（{f.type}{'，可选值：' + '、'.join(f.options) if f.options else ''}）"
            f"：{f.description or f.name}"
            for f in fields
        )
        tasks.append(
            "只有当问题里明确提到时，才抽取以下元数据字段的过滤条件；没提到的不要猜：\n" + desc
        )
        keys.append(
            '"filters": [{"field": 字段名, "op": "eq" | "in" | "gte" | "lte" | "gt" | "lt", '
            '"value": 值}]（没有就空数组）'
        )
    if datasets:
        desc = "\n".join(
            f"  {i}. {d['name']}：{d['description'] or '（无描述）'}"
            for i, d in enumerate(datasets, 1)
        )
        tasks.append(
            "从下列知识库中选出最可能包含答案的 1–3 个（给序号）；拿不准就给空数组：\n" + desc
        )
        keys.append('"datasets": [序号, ...]')
    return tasks, keys


def _history_text(history: list[dict[str, str]]) -> str:
    lines = []
    for turn in history[-_HISTORY_TURNS:]:
        role = "用户" if turn.get("role") == "user" else "助手"
        lines.append(f"{role}：{str(turn.get('content') or '')[:_HISTORY_CHARS]}")
    return "对话历史：\n" + "\n".join(lines) + "\n" if lines else ""


async def understand(
    query: str,
    *,
    cfg: dict[str, Any],
    llm: ChatModel | None,
    redis: Any = None,
    history: list[dict[str, str]] | None = None,
    fields: dict[str, FieldDef] | None = None,
    datasets: list[dict[str, str]] | None = None,
) -> QueryPlan:
    """datasets：[{id, name, description}]，只在需要路由时传。"""
    history = [t for t in history or [] if t.get("content")]
    extractable = [f for f in (fields or {}).values() if f.llm_extract]
    rewrite = bool(history) and (len(query) <= _SHORT or bool(_REFERENCE.search(query)))
    multi = bool(cfg.get("multi_query"))
    hyde = bool(cfg.get("hyde"))
    decompose = bool(cfg.get("decompose"))
    use_fields = extractable if cfg.get("filter_extraction", True) else []
    route = datasets if datasets and len(datasets) > int(cfg.get("routing_threshold", 5)) else []
    if not (rewrite or multi or hyde or decompose or use_fields or route):
        return QueryPlan.plain(query)
    if llm is None:
        return QueryPlan.plain(query, skipped="未配置 LLM")

    tasks, keys = _tasks(
        rewrite=rewrite,
        multi=multi,
        hyde=hyde,
        decompose=decompose,
        fields=use_fields,
        datasets=route,
    )
    prompt = _PROMPT.format(
        tasks="\n".join(f"{i}. {t}" for i, t in enumerate(tasks, 1)),
        context=_history_text(history) if rewrite else "",
        query=query,
        keys="\n".join(keys),
    )
    key = f"kb:qu:{llm.model}:{text_hash(prompt)}"
    raw: str | None = None
    try:
        if redis is not None:
            hit = await redis.get(key)
            raw = hit.decode() if isinstance(hit, bytes) else hit
        if raw is None:
            raw = await asyncio.wait_for(
                llm.complete([{"role": "user", "content": prompt}], max_tokens=600, json_mode=True),
                timeout=_TIMEOUT_S,
            )
            data = parse_json(raw)
            if redis is not None:
                await redis.set(key, json.dumps(data, ensure_ascii=False), ex=_CACHE_TTL_S)
        else:
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("输出不是 JSON 对象")
    except Exception as exc:
        logger.warning("查询理解失败，按原问题检索：%s", exc)
        return QueryPlan.plain(query, error=str(exc)[:200])
    return _plan(
        query,
        data,
        rewrite=rewrite,
        multi=multi,
        hyde=hyde,
        decompose=decompose,
        fields=use_fields,
        route=route,
    )


def _texts(v: Any, limit: int) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip()[:500] for x in v if str(x).strip()][:limit]


def _plan(
    query: str,
    data: dict[str, Any],
    *,
    rewrite: bool,
    multi: bool,
    hyde: bool,
    decompose: bool,
    fields: list[FieldDef],
    route: list[dict[str, str]],
) -> QueryPlan:
    main = str(data.get("rewrite") or "").strip()[:1000] if rewrite else ""
    plan = QueryPlan(original=query, query=main or query)
    notes: dict[str, Any] = {}
    if main and main != query:
        notes["rewrite"] = main
    plan.variants = [(plan.query, None)]
    if multi:
        extra = [q for q in _texts(data.get("queries"), 3) if q != plan.query]
        plan.variants += [(q, None) for q in extra]
        if extra:
            notes["queries"] = extra
    if hyde and (text := str(data.get("hyde") or "").strip()):
        plan.variants.append((text[:1000], "semantic_search"))  # 假设性答案只做语义召回
        notes["hyde"] = text[:1000]
    if decompose:
        subs = _texts(data.get("sub_questions"), 4)
        if len(subs) >= 2:
            plan.sub_questions = subs
            notes["sub_questions"] = subs
    if fields and isinstance(data.get("filters"), list):
        by_name = {f.name: f for f in fields}
        leaves = []
        for item in data["filters"][:5]:
            if not isinstance(item, dict) or item.get("field") not in by_name:
                continue
            try:
                ok = validate_filter(item, by_name)
            except Invalid:
                continue
            if ok:
                leaves.append(ok)
        if leaves:
            plan.extracted = leaves[0] if len(leaves) == 1 else {"and": leaves}
            notes["filters"] = plan.extracted
    if route and isinstance(data.get("datasets"), list):
        picked: list[str] = []
        for i in data["datasets"]:
            try:
                idx = int(i) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(route) and route[idx]["id"] not in picked:
                picked.append(route[idx]["id"])
        if picked:  # 空 = 拿不准：回退到全部库
            plan.datasets = picked
            notes["routed"] = picked
    plan.notes = notes
    return plan
