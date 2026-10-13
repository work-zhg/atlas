"""消息模型的公共基类。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

#: 不透明的 JSON 对象。ACP 的内容在上游协议里就是这个类型：运送它，但不理解它（§3 D2）。
JsonObject = dict[str, Any]


class Model(BaseModel):
    """线上是 camelCase，Python 里是 snake_case。

    ★ ``extra="ignore"``：server 与 bridge 分开发布，旧的一方必须能接受新的一方带来的
      可选字段（兼容的新增，§4.3）。不认识的字段忽略，而不是报错。
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )

    def to_wire(self) -> dict[str, Any]:
        """序列化为线上格式：camelCase，省略 None。"""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
