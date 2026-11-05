"""配置值的类型：ValueKind（四种基本类型加上 list、map）和 ValueType（kind + 元素类型）。

★ 严格校验：bool 不算 int；int 可以写进 float；map 的 key 只能是字符串；list / map 的元素只能是基本
  类型、不能为空、不能再嵌套 —— 嵌套结构在校验和管理界面上都麻烦，需要时拆成几个 key。
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["TypeMismatch", "ValueKind", "ValueType"]


class TypeMismatch(TypeError):
    """值和声明的类型对不上。"""


class ValueKind(StrEnum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    LIST = "list"
    MAP = "map"

    @property
    def scalar(self) -> bool:
        return self not in (ValueKind.LIST, ValueKind.MAP)


_SCALARS: dict[Any, ValueKind] = {
    str: ValueKind.STRING,
    int: ValueKind.INT,
    float: ValueKind.FLOAT,
    bool: ValueKind.BOOL,
}


@dataclass(frozen=True)
class ValueType:
    kind: ValueKind
    #: list / map 的元素类型（基本类型）；其余为 None
    element: ValueKind | None = None

    def __post_init__(self) -> None:
        if self.kind.scalar and self.element is not None:
            raise ValueError(f"{self.kind} 没有元素类型")
        if not self.kind.scalar and (self.element is None or not self.element.scalar):
            raise ValueError(f"{self.kind} 的元素只能是基本类型")

    @classmethod
    def of(cls, annotation: Any) -> ValueType:
        """Python 注解 → 类型：str、int、float、bool、list[T]、dict[str, T]（T 是基本类型）。"""
        if annotation in _SCALARS:
            return cls(_SCALARS[annotation])
        origin, args = typing.get_origin(annotation), typing.get_args(annotation)
        if origin is list and len(args) == 1 and args[0] in _SCALARS:
            return cls(ValueKind.LIST, _SCALARS[args[0]])
        if origin is dict and len(args) == 2 and args[0] is str and args[1] in _SCALARS:
            return cls(ValueKind.MAP, _SCALARS[args[1]])
        if origin in (typing.Union, types.UnionType):
            raise TypeError(f"配置类型不能是联合类型：{annotation!r}（空值表示用默认值）")
        raise TypeError(
            f"不支持的配置类型 {annotation!r}：只能是 str、int、float、bool、list[T]、dict[str, T]"
        )

    def describe(self) -> str:
        return (
            self.kind.value if self.element is None else f"{self.kind.value}<{self.element.value}>"
        )

    def validate(self, value: Any) -> Any:
        """按类型校验并规整（int 写进 float 时转成 float）。不合规抛 TypeMismatch。"""
        if self.kind is ValueKind.LIST:
            if not isinstance(value, list):
                raise TypeMismatch(
                    f"要一个列表（{self.describe()}），拿到的是 {type(value).__name__}"
                )
            return [_scalar(self.element, v, where=f"[{i}]") for i, v in enumerate(value)]  # type: ignore[arg-type]
        if self.kind is ValueKind.MAP:
            if not isinstance(value, dict):
                raise TypeMismatch(
                    f"要一个映射（{self.describe()}），拿到的是 {type(value).__name__}"
                )
            out = {}
            for k, v in value.items():
                if not isinstance(k, str):
                    raise TypeMismatch(f"映射的 key 只能是字符串：{k!r}")
                out[k] = _scalar(self.element, v, where=f"[{k!r}]")  # type: ignore[arg-type]
            return out
        return _scalar(self.kind, value, where="")


def _scalar(kind: ValueKind, value: Any, *, where: str) -> Any:
    if value is None:
        raise TypeMismatch(f"值{where}不能为空")
    if kind is ValueKind.BOOL:
        ok = isinstance(value, bool)
    elif kind is ValueKind.INT:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind is ValueKind.FLOAT:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        if ok:
            value = float(value)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise TypeMismatch(
            f"值{where} 要是 {kind.value}，拿到的是 {type(value).__name__}：{value!r}"
        )
    return value
