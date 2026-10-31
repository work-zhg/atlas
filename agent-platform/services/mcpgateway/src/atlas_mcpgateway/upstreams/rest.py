"""REST 转换：OpenAPI 的一个操作（operation）= 一个工具。

映射（mcp_tool.mapping）：
    method     GET | POST | PUT | PATCH | DELETE
    path       相对 Base URL，{占位符} 取位置为 path 的参数
    params     [{name, in, type, required, fixed, description, field}]
                 name   工具参数名（给模型看）
                 in     path | query | header | body
                 fixed  固定值：每次都按它发给上游，不出现在工具参数里
                 field  上游的字段名（缺省 = name；和别的位置重名时 name 会加前缀）；
                        body 的 field 为 "$" 表示整个请求体
    pick       只取响应里的字段（$.items、$.data.list[0] …），空 = 整个响应体
    limit_kb   结果上限，超过截断并注明

★ 支持 OpenAPI 3.x 与 Swagger 2.0，JSON 或 YAML；只解析本文档内的 $ref。
★ 只支持 JSON 请求体、JSON / 文本响应：文件上传、表单、二进制下载标成「不支持」，不能选。
★ 非 2xx 作为工具错误返回（isError = true，带状态码和响应摘要），交给模型处理。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from ..definitions import PARAM_TYPES, TOOL_NAME_PATTERN
from ..errors import Invalid
from . import DiscoveryFailed, UpstreamUnavailable, describe_error, tool_result, trace_headers

__all__ = [
    "METHODS",
    "Operation",
    "call",
    "check_name",
    "effect_of_method",
    "fetch_spec",
    "load_spec",
    "parse",
    "validate",
]

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_LOCATIONS = ("path", "query", "header", "body")
_DEFAULT_LIMIT_KB = 32
_MAX_LIMIT_KB = 1024
_TOOL_NAME = re.compile(TOOL_NAME_PATTERN)
_PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


def effect_of_method(method: str) -> str:
    return "read" if method == "GET" else "destructive" if method == "DELETE" else "write"


@dataclass
class Operation:
    """OpenAPI 里的一个操作（解析结果，登记前给管理员挑）。"""

    method: str
    path: str
    name: str
    description: str
    params: list[dict[str, Any]] = field(default_factory=list)
    #: 不支持的原因；None = 能转换
    unsupported: str | None = None

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def effect(self) -> str:
        return effect_of_method(self.method)

    def mapping(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "params": self.params,
            "pick": "",
            "limit_kb": _DEFAULT_LIMIT_KB,
        }


# ───────────────────────────── 解析 OpenAPI


async def fetch_spec(
    http: httpx.AsyncClient, url: str, *, headers: Mapping[str, str], query: Mapping[str, str]
) -> dict[str, Any]:
    try:
        r = await http.get(url, headers=dict(headers), params=dict(query), timeout=15.0)
    except httpx.HTTPError as exc:
        raise DiscoveryFailed(f"取不到接口定义：{describe_error(exc)}") from exc
    if r.status_code >= 400:
        raise DiscoveryFailed(f"取接口定义返回 HTTP {r.status_code}")
    return load_spec(r.text)


def load_spec(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except ValueError:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise DiscoveryFailed("接口定义既不是 JSON 也不是 YAML") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
        raise DiscoveryFailed("不是 OpenAPI 文档（没有 paths）")
    return doc


def parse(doc: Mapping[str, Any]) -> list[Operation]:
    swagger2 = str(doc.get("swagger") or "").startswith("2")
    ops: list[Operation] = []
    taken: set[str] = set()
    for path, item in doc["paths"].items():
        if not isinstance(item, dict):
            continue
        common = _deref(doc, item).get("parameters") or []
        for method in METHODS:
            raw = item.get(method.lower())
            if isinstance(raw, dict):
                op = _operation(doc, str(path), method, raw, common, swagger2=swagger2)
                op.name = _unique(op.name, taken)
                ops.append(op)
    return ops


def _deref(doc: Mapping[str, Any], obj: Any, depth: int = 0) -> Any:
    while isinstance(obj, dict) and isinstance(obj.get("$ref"), str) and depth < 10:
        ref = obj["$ref"]
        if not ref.startswith("#/"):
            return {}
        node: Any = doc
        for part in ref[2:].split("/"):
            node = (
                node.get(part.replace("~1", "/").replace("~0", "~"))
                if isinstance(node, dict)
                else None
            )
        obj, depth = node or {}, depth + 1
    return obj


def _type(schema: Mapping[str, Any]) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t in PARAM_TYPES:
        return str(t)
    if "properties" in schema:
        return "object"
    return "string"


def _snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    text = re.sub(r"_+", "_", text)
    if not text or not text[0].isalpha():
        text = f"op_{text}".rstrip("_")
    return text[:64]


def _unique(name: str, taken: set[str]) -> str:
    base, n = name, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[: 64 - len(suffix)] + suffix, n + 1
    taken.add(name)
    return name


def _operation(
    doc: Mapping[str, Any],
    path: str,
    method: str,
    raw: Mapping[str, Any],
    common: Sequence[Any],
    *,
    swagger2: bool,
) -> Operation:
    op_id = raw.get("operationId")
    name = _snake(str(op_id)) if op_id else _snake(f"{method.lower()}_{path}")
    summary = str(raw.get("summary") or "").strip()
    detail = str(raw.get("description") or "").strip()
    description = "\n".join(x for x in (summary, detail) if x)[:2000]
    params: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for p in [
        *(_deref(doc, x) for x in common),
        *(_deref(doc, x) for x in raw.get("parameters") or []),
    ]:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        loc = p.get("in")
        if loc in ("path", "query", "header"):
            schema = p if swagger2 else _deref(doc, p.get("schema") or {})
            seen[(str(p["name"]), str(loc))] = {
                "name": str(p["name"]),
                "in": loc,
                "type": _type(schema),
                "required": bool(p.get("required")) or loc == "path",
                "description": str(p.get("description") or "")[:500],
            }
        elif loc == "body":
            _body_params(doc, _deref(doc, p.get("schema") or {}), seen)
        elif loc == "formData":
            problems.append("表单参数（formData）")
        elif loc == "cookie":
            problems.append("cookie 参数")
    if not swagger2 and raw.get("requestBody"):
        body = _deref(doc, raw["requestBody"])
        content = body.get("content") or {}
        json_type = next(
            (c for c in content if c == "application/json" or c.endswith("+json")), None
        )
        if json_type is not None:
            _body_params(doc, _deref(doc, content[json_type].get("schema") or {}), seen)
        elif content:
            problems.append(f"请求体是 {next(iter(content))}")
    response_types = _response_types(doc, raw, swagger2=swagger2)
    if response_types and not any(_textual(t) for t in response_types):
        problems.append(f"响应是 {response_types[0]}，不是 JSON 或文本")
    # 不同位置的同名参数：后来的加位置前缀，field 记上游的名字
    names: set[str] = set()
    for (pname, loc), p in seen.items():
        tool_name = pname if pname not in names else f"{loc}_{pname}"
        names.add(tool_name)
        whole = p.pop("whole", False)
        params.append({**p, "name": tool_name, "field": "$" if whole else pname, "fixed": ""})
    return Operation(
        method=method,
        path=path,
        name=name,
        description=description,
        params=params,
        unsupported="；".join(problems) or None,
    )


def _body_params(
    doc: Mapping[str, Any], schema: Mapping[str, Any], seen: dict[tuple[str, str], dict[str, Any]]
) -> None:
    props = schema.get("properties")
    if isinstance(props, dict) and props:
        required = set(schema.get("required") or [])
        for pname, prop in props.items():
            prop = _deref(doc, prop)
            seen[(str(pname), "body")] = {
                "name": str(pname),
                "in": "body",
                "type": _type(prop),
                "required": pname in required,
                "description": str(prop.get("description") or "")[:500],
            }
    elif schema:
        seen[("body", "body")] = {
            "name": "body",
            "in": "body",
            "type": _type(schema),
            "required": True,
            "description": "请求体",
            "whole": True,
        }


def _response_types(doc: Mapping[str, Any], raw: Mapping[str, Any], *, swagger2: bool) -> list[str]:
    out: list[str] = []
    if swagger2:
        return [str(t) for t in raw.get("produces") or doc.get("produces") or []]
    for code, resp in (raw.get("responses") or {}).items():
        if str(code).startswith("2"):
            out.extend(str(c) for c in (_deref(doc, resp).get("content") or {}))
    return out


def _textual(content_type: str) -> bool:
    ct = content_type.lower()
    return "json" in ct or ct.startswith("text/") or ct == "*/*"


# ───────────────────────────── 校验（管理员在网关里写的映射）


def validate(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """→ 规整过的映射；不合规抛 Invalid（说明给管理员看）。"""
    method = str(mapping.get("method") or "").upper()
    if method not in METHODS:
        raise Invalid(f"Method 只能是 {'、'.join(METHODS)}")
    path = str(mapping.get("path") or "").strip()
    if not path.startswith("/"):
        raise Invalid("路径要以 / 开头")
    params: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in mapping.get("params") or []:
        name = str(raw.get("name") or "").strip()
        if not _PARAM_NAME.match(name):
            raise Invalid(
                f"参数名 {name!r} 不合规（字母、数字、下划线、点、连字符，字母或下划线开头）"
            )
        if name in names:
            raise Invalid(f"参数名重复：{name}")
        names.add(name)
        loc = str(raw.get("in") or "")
        if loc not in _LOCATIONS:
            raise Invalid(f"参数 {name} 的位置只能是 {'、'.join(_LOCATIONS)}")
        ptype = str(raw.get("type") or "string")
        if ptype not in PARAM_TYPES:
            raise Invalid(f"参数 {name} 的类型只能是 {'、'.join(PARAM_TYPES)}")
        params.append(
            {
                "name": name,
                "in": loc,
                "type": ptype,
                "required": bool(raw.get("required")) or loc == "path",
                "fixed": str(raw.get("fixed") or "").strip(),
                "description": str(raw.get("description") or "").strip()[:500],
                "field": str(raw.get("field") or name).strip() or name,
            }
        )
    holes = re.findall(r"\{([^}]+)\}", path)
    path_fields = [p["field"] for p in params if p["in"] == "path"]
    missing = [h for h in holes if h not in path_fields]
    if missing:
        raise Invalid(f"路径里的 {{{missing[0]}}} 没有对应的 path 参数")
    extra = [f for f in path_fields if f not in holes]
    if extra:
        raise Invalid(f"path 参数 {extra[0]} 没出现在路径里")
    limit = int(mapping.get("limit_kb") or _DEFAULT_LIMIT_KB)
    if not 1 <= limit <= _MAX_LIMIT_KB:
        raise Invalid(f"结果上限要在 1–{_MAX_LIMIT_KB} KB 之间")
    pick = str(mapping.get("pick") or "").strip()
    if pick and not re.match(r"^\$(\.[A-Za-z0-9_-]+|\[\d+\])*$", pick):
        raise Invalid("只取响应里的字段：写成 $.items、$.data.list[0] 这样")
    return {"method": method, "path": path, "params": params, "pick": pick, "limit_kb": limit}


def check_name(name: str) -> None:
    if not _TOOL_NAME.match(name):
        raise Invalid("工具名：小写字母开头，只能有小写字母、数字、下划线，不超过 64 个字符")


# ───────────────────────────── 调用


def _pick(data: Any, expr: str) -> Any:
    if not expr or expr == "$":
        return data
    node = data
    for name, index in re.findall(r"\.([A-Za-z0-9_-]+)|\[(\d+)\]", expr):
        if name and isinstance(node, dict) and name in node:
            node = node[name]
        elif index and isinstance(node, list) and int(index) < len(node):
            node = node[int(index)]
        else:
            return data  # 路径不在响应里：返回整个响应，模型自己看
    return node


def _limit(text: str, limit_kb: int) -> str:
    cap = limit_kb * 1024
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…（结果超过 {limit_kb} KB，已截断）"


async def call(
    http: httpx.AsyncClient,
    base_url: str,
    mapping: Mapping[str, Any],
    arguments: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    query: Mapping[str, str],
) -> dict[str, Any]:
    """按映射调一次上游 → MCP 线上格式的 CallToolResult。连不上抛 UpstreamUnavailable。"""
    path_values: dict[str, str] = {}
    q: dict[str, Any] = {}
    h: dict[str, str] = {}
    body: Any = None
    for p in mapping.get("params") or []:
        value = p.get("fixed") or arguments.get(p["name"])
        if value is None or value == "":
            continue
        target = p.get("field") or p["name"]
        match p["in"]:
            case "path":
                path_values[target] = str(value)
            case "query":
                q[target] = value
            case "header":
                h[target] = str(value)
            case "body":
                if target == "$":
                    body = value
                else:
                    body = body if isinstance(body, dict) else {}
                    body[target] = value
    url_path = re.sub(
        r"\{([^}]+)\}", lambda m: quote(path_values.get(m.group(1), ""), safe=""), mapping["path"]
    )
    url = base_url.rstrip("/") + url_path
    try:
        r = await http.request(
            mapping["method"],
            url,
            params={**q, **query},
            headers={**h, **headers, **trace_headers()},
            json=body,
        )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(describe_error(exc)) from exc
    limit_kb = int(mapping.get("limit_kb") or _DEFAULT_LIMIT_KB)
    content_type = r.headers.get("content-type", "").lower()
    if r.status_code >= 300:
        excerpt = r.text[:2000]
        return tool_result(f"HTTP {r.status_code}\n{excerpt}".strip(), is_error=True)
    if "json" in content_type:
        try:
            data = _pick(r.json(), str(mapping.get("pick") or ""))
        except ValueError:
            return tool_result(_limit(r.text, limit_kb))
        text = _limit(json.dumps(data, ensure_ascii=False), limit_kb)
        return tool_result(text, structured=data if isinstance(data, dict) else None)
    return tool_result(_limit(r.text, limit_kb))
