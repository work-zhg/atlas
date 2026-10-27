"""请求体（对外契约）。响应是 store.py / scopes.py 的 *_view 字典。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "MoveIn",
    "RegisterIn",
    "RestoreIn",
    "ScopeCreate",
    "ScopePatch",
    "SplitGroupIn",
    "SplitIn",
    "TopicCreate",
    "TopicPatch",
    "UndoIn",
    "WriteIn",
]


class ScopeCreate(BaseModel):
    code: str
    description: str = ""
    #: 关联主键的字段名，有序，例如 ["uid"]、["uid", "project"]。建好后不能改
    key_fields: list[str]
    check_permission: bool = False
    #: 用户中心数据编码；开关打开且不给时按编码生成（user_project → MemoryUserProject）
    data_code: str | None = None


class ScopePatch(BaseModel):
    description: str | None = None
    check_permission: bool | None = None
    data_code: str | None = None


class RegisterIn(BaseModel):
    #: 实例的 Owner（用户中心用户 id）。★ 显式给出，不从请求头推断（§3.7）
    owner_uid: str = Field(min_length=1, max_length=64)


class TopicCreate(BaseModel):
    code: str
    description: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)


class TopicPatch(BaseModel):
    description: str | None = None
    attrs: dict[str, Any] | None = None


class WriteIn(BaseModel):
    """一次写入：删除若干旧条目 + 至多新增一条，在一个事务里完成（§3.5）。"""

    #: 新增条目的目标主题编码（只删除时可以不给）
    topic: str | None = None
    #: 新增的内容；不给 = 只删除（遗忘）
    content: str | None = None
    #: 要删除的条目编号
    remove: list[int] = Field(default_factory=list)
    #: 这条信息成立的时间（带时区），用来判断先后：以时间晚的为准
    occurred_at: datetime
    #: 来源标签，原样保存（Atlas：user / agent / backfill）
    source: str = Field(default="", max_length=32)
    #: 出处，原样保存（例如在哪个会话、哪一轮写入的）
    origin: dict[str, Any] | None = None
    #: 幂等键：同一个键重复提交只写一次，返回第一次的结果
    idempotency_key: str | None = Field(default=None, max_length=128)


class MoveIn(BaseModel):
    entries: list[int]
    to_topic: str


class SplitGroupIn(BaseModel):
    code: str
    description: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)
    entries: list[int]


class SplitIn(BaseModel):
    #: 客户端分组时看到的主题版本号；提交时不一致就拒绝（分组期间有新的写入）
    expected_version: int
    groups: list[SplitGroupIn]


class UndoIn(BaseModel):
    source: str = Field(default="", max_length=32)
    origin: dict[str, Any] | None = None


class RestoreIn(BaseModel):
    #: 恢复到哪个主题；不给 = 原主题（原主题已拆分时放进拆出来的主题）
    topic: str | None = None
    source: str = Field(default="", max_length=32)
    origin: dict[str, Any] | None = None
