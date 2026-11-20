"""ConfigKey：代码里的一个配置声明，通常写成模块级常量。

    MAX_PODS = ConfigKey("sandbox.max_pods", int, default=10, description="每人同时几个沙箱")

类型从 Python 注解得出，泛型参数让 AppConfig.get() 的返回值有静态类型。

作用域（scope）：SYSTEM 是系统参数，全系统一份，只有运营中心能改；TENANT 是租户设置，每个租户
一份，读的时候按「本租户的值 → 系统值 → 代码默认值」取。登记后不能改，要改就换一个 key 名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .types import ValueType

__all__ = ["ConfigKey", "ConfigScope"]


class ConfigScope(StrEnum):
    #: 系统参数：全系统一份，不含任何租户内容，只有运营中心能改
    SYSTEM = "system"
    #: 租户设置：每个租户一份，没改过的租户用系统值
    TENANT = "tenant"


#: 名字只能用小写字母、数字、点、下划线和连字符
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True, init=False)
class ConfigKey[T]:
    name: str
    type: ValueType
    default: T
    description: str = field(default="")
    scope: ConfigScope = field(default=ConfigScope.SYSTEM)

    def __init__(
        self,
        name: str,
        annotation: Any,
        *,
        default: T,
        description: str = "",
        scope: ConfigScope = ConfigScope.SYSTEM,
    ) -> None:
        if not NAME.match(name):
            raise ValueError(f"配置名只能用小写字母、数字、点、下划线和连字符：{name!r}")
        value_type = ValueType.of(annotation)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "type", value_type)
        object.__setattr__(self, "default", value_type.validate(default))
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "scope", ConfigScope(scope))
