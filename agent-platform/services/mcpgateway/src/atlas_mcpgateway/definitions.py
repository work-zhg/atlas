"""工具定义：规范化、检查、定义指纹、副作用与注解。

    MCP 代理      定义从上游拉来（tools/list），副作用取上游注解
    REST / gRPC  定义由管理员在网关里写（映射 + 参数），副作用由管理员定，注解由副作用算出

工具定义（名字、描述、参数说明）会原样进入模型上下文。

★ 指纹的算法沿用：没标只读的工具 = sha256([name, description, schema])，标了只读的把注解算进去。
  Atlas 保存智能体时记下指纹、据此提示定义变化，改算法会让存量指纹全变。
★ 只做「不合规就用不了」的检查（工具名不符合模型侧规则、参数定义不是对象）。没有复核：
  上游新增、改了的工具，拉到就生效。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PARAM_TYPES",
    "SERVICE_NAME_PATTERN",
    "TOOL_NAME_PATTERN",
    "Definition",
    "annotations_for",
    "check",
    "content_hash",
    "define",
    "digest_of",
    "effect_of",
    "model_name",
    "normalize",
    "read_only",
    "schema_from_params",
]

#: 服务名：小写字母开头、不含下划线（模型看到的名字是「服务名__工具名」，第一个 __ 就是分隔点），
#: 不超过 24 个字符
SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9-]{1,23}$"
#: 网关里定义的工具（REST / gRPC）的名字
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
#: 各模型厂商工具名规则的共同子集
_MODEL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: REST 参数的类型（JSON Schema）
PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")


def model_name(service: str, tool: str) -> str | None:
    """模型看到的名字。不合规（字符集 / 超长）时返回 None。"""
    name = f"{service}__{tool}"
    return name if _MODEL_NAME.match(name) else None


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_only(annotations: Mapping[str, Any] | None) -> bool:
    return (annotations or {}).get("readOnlyHint") is True


def digest_of(
    name: str, description: str, schema: Mapping[str, Any], annotations: Mapping[str, Any] | None
) -> str:
    parts: list[Any] = [name, description, dict(schema)]
    if read_only(annotations):
        parts.append({"readOnlyHint": True})
    return _sha(_canonical(parts))


def content_hash(digests: Iterable[str]) -> str:
    return _sha(_canonical(sorted(digests)))


def effect_of(annotations: Mapping[str, Any] | None) -> str:
    """上游注解 → 副作用。★ 上游没标注的按「有副作用」处理（不按 MCP 默认的破坏性）。"""
    a = annotations or {}
    if a.get("readOnlyHint") is True:
        return "read"
    if a.get("destructiveHint") is True:
        return "destructive"
    return "write"


def annotations_for(effect: str) -> dict[str, Any]:
    """副作用 → MCP 工具注解（REST / gRPC 的工具）。"""
    if effect == "read":
        return {"readOnlyHint": True}
    return {"readOnlyHint": False, "destructiveHint": effect == "destructive"}


@dataclass(frozen=True)
class Definition:
    """一个工具的定义，已规范化（MCP 线上格式的注解）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None
    digest: str
    issues: tuple[str, ...]

    @property
    def effect(self) -> str:
        return effect_of(self.annotations)


def check(service: str, name: str, schema: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if model_name(service, name) is None:
        issues.append(f"工具名 {name!r} 不符合模型侧命名规则（[A-Za-z0-9_-]，连同服务名总长 ≤64）")
    if schema.get("type") != "object":
        issues.append("参数定义不是对象（inputSchema.type 不是 object）")
    return issues


def define(
    service: str,
    name: str,
    description: str,
    schema: Mapping[str, Any],
    annotations: Mapping[str, Any] | None,
) -> Definition:
    annotations = {k: v for k, v in (annotations or {}).items() if v is not None} or None
    return Definition(
        name=name,
        description=description,
        input_schema=dict(schema),
        annotations=annotations,
        digest=digest_of(name, description, schema, annotations),
        issues=tuple(check(service, name, schema)),
    )


def normalize(service: str, raw: Mapping[str, Any]) -> Definition:
    """MCP 线上格式的工具（上游 tools/list 返回里的一项）→ Definition。"""
    schema = raw.get("inputSchema")
    annotations = raw.get("annotations")
    return define(
        service,
        str(raw.get("name") or ""),
        str(raw.get("description") or ""),
        dict(schema) if isinstance(schema, dict) else {},
        dict(annotations) if isinstance(annotations, dict) else None,
    )


def schema_from_params(params: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """REST 工具的参数（映射里的 params）→ inputSchema。固定值参数不进参数定义。"""
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in params:
        if p.get("fixed") not in (None, ""):
            continue
        prop: dict[str, Any] = {"type": p.get("type") or "string"}
        if prop["type"] == "array":
            prop["items"] = {"type": "string"}
        if p.get("description"):
            prop["description"] = str(p["description"])
        props[str(p["name"])] = prop
        if p.get("required"):
            required.append(str(p["name"]))
    return {"type": "object", "properties": props, **({"required": required} if required else {})}
