"""检索范围与过滤（doc/knowledge-enterprise-design.html §7.2 / §7.4 / §7.5）→ Qdrant Filter。

    文档级权限   acl 与调用者的主体标签有交集，或为 public（没有 acl 键的老 point 按 public）
    版本         默认只要当前版本（is_current 不为 false）；
                 as_of 按时间点检索，include_superseded 连历史版本一起查
    时效         t 落在 [valid_from, valid_to)（没有的键 = 不限）；t = as_of 或现在
    元数据       过滤表达式（and / or / 叶子），只作用于业务元数据 meta.*

过滤表达式：
    {"and": [expr, ...]}   {"or": [expr, ...]}
    {"field": "year", "op": "eq" | "ne" | "in" | "gt" | "gte" | "lt" | "lte" | "range" | "exists",
     "value": ...}        range 的 value = {"gte": .., "lt": ..}；exists 的 value = true / false

★ 多库检索时，叶子引用了某库没定义的字段：这个叶子在该库视为「不限」（§7.2）。
★ 权限、版本由服务端强制附加，与调用方传的过滤表达式、LLM 抽取的条件无关。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from qdrant_client import models

from ..errors import Invalid

__all__ = [
    "BUILTIN_FIELDS",
    "FieldDef",
    "Scope",
    "compile_filter",
    "field_index_kind",
    "normalize_value",
    "scope_filter",
    "validate_filter",
]

_OPS = ("eq", "ne", "in", "gt", "gte", "lt", "lte", "range", "exists")
_MAX_DEPTH = 5
_MAX_LEAVES = 30


@dataclass(frozen=True)
class FieldDef:
    name: str
    #: string | number | date | enum
    type: str
    multi: bool = False
    options: tuple[str, ...] = ()
    description: str = ""
    llm_extract: bool = False


#: 内置字段（每个文档自动带上，写进 meta）
BUILTIN_FIELDS: dict[str, FieldDef] = {
    "doc_type": FieldDef("doc_type", "string", description="文档类型（扩展名，如 pdf、docx）"),
    "author": FieldDef("author", "string", description="上传人"),
    "uploaded_at": FieldDef("uploaded_at", "date", description="上传时间"),
}


@dataclass
class Scope:
    """一次检索的范围。principals = None 表示不做文档级权限过滤（库管理员调试）。"""

    principals: list[str] | None = None
    as_of: datetime | None = None
    include_superseded: bool = False
    #: 元数据过滤表达式（已校验）
    filter: dict[str, Any] | None = None
    #: 查询理解抽出的过滤条件（只收窄：与 filter 取与）
    extracted: dict[str, Any] | None = None
    #: 记录用：本次附加了哪些范围条件
    notes: dict[str, Any] = field(default_factory=dict)


def field_index_kind(f: FieldDef) -> str:
    return {"number": "float", "date": "datetime"}.get(f.type, "keyword")


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=UTC)
    elif isinstance(v, date):
        dt = datetime(v.year, v.month, v.day, tzinfo=UTC)
    else:
        text = str(v).strip().replace("Z", "+00:00")
        if len(text) == 4 and text.isdigit():  # 只有年份
            text = f"{text}-01-01"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise Invalid(f"日期格式不对：{v}") from exc
        dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_value(f: FieldDef, value: Any) -> Any:
    """按字段类型规范化一个值（文档元数据赋值、过滤表达式里的值都走这里）。"""
    if isinstance(value, list):
        return [normalize_value(f, v) for v in value]
    if f.type == "number":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise Invalid(f"字段 {f.name} 要求数字，收到 {value!r}") from exc
    if f.type == "date":
        return _iso(value)
    text = str(value).strip()
    if f.type == "enum" and f.options and text not in f.options:
        raise Invalid(f"字段 {f.name} 的取值只能是 {'、'.join(f.options)}")
    return text


def validate_filter(expr: Any, fields: dict[str, FieldDef]) -> dict[str, Any] | None:
    """校验并规范化过滤表达式。fields = 所有相关库的字段并集（含内置字段）。"""
    if expr in (None, {}):
        return None
    leaves = 0

    def walk(e: Any, depth: int) -> dict[str, Any]:
        nonlocal leaves
        if not isinstance(e, dict):
            raise Invalid("过滤表达式必须是对象")
        if depth > _MAX_DEPTH:
            raise Invalid("过滤表达式嵌套过深")
        for key in ("and", "or"):
            if key in e:
                items = e[key]
                if not isinstance(items, list) or not items:
                    raise Invalid(f"{key} 需要非空数组")
                return {key: [walk(x, depth + 1) for x in items]}
        leaves += 1
        if leaves > _MAX_LEAVES:
            raise Invalid("过滤条件过多")
        name, op = e.get("field"), e.get("op", "eq")
        if op not in _OPS:
            raise Invalid(f"不支持的过滤操作：{op}")
        f = fields.get(str(name))
        if f is None:
            raise Invalid(f"没有元数据字段：{name}")
        value = e.get("value")
        if op == "exists":
            return {"field": f.name, "op": op, "value": bool(True if value is None else value)}
        if op == "in":
            if not isinstance(value, list) or not value:
                raise Invalid("in 的值必须是非空数组")
            return {"field": f.name, "op": op, "value": normalize_value(f, value)}
        if op in ("gt", "gte", "lt", "lte", "range"):
            if f.type not in ("number", "date"):
                raise Invalid(f"字段 {f.name} 不是数字或日期，不能比较大小")
            bounds = value if op == "range" else {op: value}
            if not isinstance(bounds, dict) or not bounds:
                raise Invalid("range 的值必须是 {gte / gt / lte / lt: 值}")
            if set(bounds) - {"gt", "gte", "lt", "lte"}:
                raise Invalid("range 只支持 gt / gte / lt / lte")
            return {
                "field": f.name,
                "op": "range",
                "value": {k: normalize_value(f, v) for k, v in bounds.items()},
            }
        return {"field": f.name, "op": op, "value": normalize_value(f, value)}

    return walk(expr, 0)


def compile_filter(expr: dict[str, Any] | None, fields: dict[str, FieldDef]) -> Any:
    """表达式 → Qdrant 条件；None = 不限。fields = 这个库的字段（含内置）。"""
    if not expr:
        return None
    if "and" in expr:
        parts = [p for p in (compile_filter(x, fields) for x in expr["and"]) if p is not None]
        return models.Filter(must=parts) if parts else None
    if "or" in expr:
        parts = [compile_filter(x, fields) for x in expr["or"]]
        if any(p is None for p in parts):
            return None
        return models.Filter(should=parts)
    f = fields.get(expr["field"])
    if f is None:  # 本库没定义这个字段：不限
        return None
    key, op, value = f"meta.{f.name}", expr["op"], expr["value"]
    if op == "exists":
        empty = models.IsEmptyCondition(is_empty=models.PayloadField(key=key))
        return models.Filter(must_not=[empty]) if value else models.Filter(must=[empty])
    if op == "range":
        rng: Any = models.DatetimeRange(**value) if f.type == "date" else models.Range(**value)
        return models.FieldCondition(key=key, range=rng)
    if f.type == "number":  # 数字的相等用区间表示（Qdrant 只能精确匹配整数与字符串）
        values = value if op == "in" else [value]
        conds = [models.FieldCondition(key=key, range=models.Range(gte=v, lte=v)) for v in values]
        cond: Any = conds[0] if len(conds) == 1 else models.Filter(should=conds)
    elif op == "in":
        cond = models.FieldCondition(key=key, match=models.MatchAny(any=value))
    else:
        cond = models.FieldCondition(key=key, match=models.MatchValue(value=value))
    return models.Filter(must_not=[cond]) if op == "ne" else cond


def scope_filter(scope: Scope, fields: dict[str, FieldDef]) -> Any:
    """检索范围 → 附加到召回上的 Qdrant Filter（与 enabled / doc_ok 取与）。"""
    must: list[Any] = []
    if scope.principals is not None:
        must.append(
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="acl", match=models.MatchAny(any=[*scope.principals, "public"])
                    ),
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="acl")),
                ]
            )
        )
    # 按时间点检索时看生效区间（被取代的旧版本入库时已写了 valid_to）；否则只要当前版本
    if scope.as_of is None and not scope.include_superseded:
        must.append(
            models.Filter(
                must_not=[
                    models.FieldCondition(key="is_current", match=models.MatchValue(value=False))
                ]
            )
        )
    t = scope.as_of.timestamp() if scope.as_of else time.time()
    if not scope.include_superseded or scope.as_of is not None:
        must.append(
            models.Filter(
                should=[
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="valid_from")),
                    models.FieldCondition(key="valid_from", range=models.Range(lte=t)),
                ]
            )
        )
        must.append(
            models.Filter(
                should=[
                    models.IsEmptyCondition(is_empty=models.PayloadField(key="valid_to")),
                    models.FieldCondition(key="valid_to", range=models.Range(gt=t)),
                ]
            )
        )
    for expr in (scope.filter, scope.extracted):
        cond = compile_filter(expr, fields)
        if cond is not None:
            must.append(cond)
    return models.Filter(must=must) if must else None
