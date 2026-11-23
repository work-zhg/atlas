"""ArgumentCheck：按工具的参数定义查必填和顶层类型，拦住模型最常见的错误。

★ 不做严格模式：上游的定义常常不完整，多给的参数不拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["check_arguments"]

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def check_arguments(schema: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    """→ 问题说明，没问题为 None。"""
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name in schema.get("required") or []:
        if name not in args or args[name] is None:
            return f"缺少必填参数 {name}"
    for name, value in args.items():
        prop = props.get(name)
        expected = prop.get("type") if isinstance(prop, dict) else None
        if not isinstance(expected, str) or expected not in _JSON_TYPES or value is None:
            continue
        ok = isinstance(value, _JSON_TYPES[expected])
        if expected in ("integer", "number") and isinstance(value, bool):
            ok = False
        if expected == "integer" and isinstance(value, float) and value.is_integer():
            ok = True
        if not ok:
            return f"参数 {name} 应该是 {expected}"
    return None
