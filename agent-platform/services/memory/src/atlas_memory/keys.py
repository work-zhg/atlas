"""关联主键（§3.2）：作用域定义字段名，调用方给值；两者一起确定一个作用域实例。

    作用域 user          key_fields = ["uid"]              路径里写 uid=6f0c…
    作用域 user_project  key_fields = ["uid", "project"]   路径里写 uid=6f0c…,project=atlas

★ 服务端不解释值是什么（用户 uid、项目编码还是别的），只按字段名核对形状、按值定位实例。
★ 值里不能有 , = / 和空白：它们是路径写法的分隔符。规范形式按作用域的字段顺序拼接，
  所以调用方写字段的先后不影响定位。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .errors import Invalid

__all__ = ["FIELD_RE", "canonical", "parse_key", "validate_fields", "validate_key"]

FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_VALUE_RE = re.compile(r"^[^\s,=/]{1,128}$")


def validate_fields(fields: list[str]) -> list[str]:
    if not fields:
        raise Invalid("关联主键至少要有一个字段")
    if len(fields) != len(set(fields)):
        raise Invalid("关联主键的字段名不能重复")
    for f in fields:
        if not FIELD_RE.match(f):
            raise Invalid(f"字段名 {f!r} 不合法：小写字母开头，只含小写字母、数字、下划线")
    return list(fields)


def validate_key(fields: list[str], key: Mapping[str, str]) -> dict[str, str]:
    """核对 key 的字段与作用域一致、值合法；返回按作用域字段顺序排好的副本。"""
    if set(key) != set(fields):
        raise Invalid(
            f"关联主键的字段应为 {', '.join(fields)}", code="BAD_KEY", expected=list(fields)
        )
    out: dict[str, str] = {}
    for f in fields:
        value = str(key[f])
        if not _VALUE_RE.match(value):
            raise Invalid(
                f"关联主键 {f} 的值不合法：1–128 个字符，不能含空白和 , = /", code="BAD_KEY"
            )
        out[f] = value
    return out


def parse_key(fields: list[str], raw: str) -> dict[str, str]:
    """路径写法 "uid=…,project=…" → {字段: 值}。"""
    key: dict[str, str] = {}
    for part in raw.split(","):
        name, sep, value = part.partition("=")
        if not sep or name in key:
            raise Invalid(
                "关联主键写成 字段=值，多个字段用逗号分隔，例如 uid=…,project=…",
                code="BAD_KEY",
                expected=list(fields),
            )
        key[name] = value
    return validate_key(fields, key)


def canonical(fields: list[str], key: Mapping[str, str]) -> str:
    return ",".join(f"{f}={key[f]}" for f in fields)
