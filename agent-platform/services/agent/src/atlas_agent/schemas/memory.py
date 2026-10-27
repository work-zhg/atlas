"""记忆管理接口的形状（doc/memory-design.html §7）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "BackfillListOut",
    "BackfillOut",
    "BackfillSeenIn",
    "EntryCreate",
    "EntryDetailOut",
    "EntryListOut",
    "EntryOut",
    "EntryRestoreIn",
    "EntryUpdate",
    "MemorySettingsIn",
    "MemorySettingsOut",
    "MoveIn",
    "PurgeOut",
    "RevisionListOut",
    "RevisionOut",
    "TopicDetailOut",
    "TopicListOut",
    "TopicOut",
    "TopicPatch",
    "WriteOut",
]


class TopicOut(BaseModel):
    scope: str
    code: str
    description: str
    #: 常驻：主题的全部条目每次对话都放进系统提示词
    resident: bool
    version: int
    entry_count: int
    char_count: int
    #: 超过了 Atlas 的主题大小上限（软上限：下次会话后补记时拆小）
    over_limit: bool


class TopicListOut(BaseModel):
    data: list[TopicOut]
    topic_max_chars: int


class EntryOut(BaseModel):
    number: int
    content: str
    topic: str | None
    #: 信息成立的时间
    occurred_at: str
    #: user = 用户亲自 / agent = Agent 写入 / backfill = 会话后补记
    source: str
    #: 出处，例如 {"thread": 会话 id, "run": run id}
    origin: dict[str, Any] | None = None
    #: active | deleted
    status: str
    superseded_by: int | None = None
    retired_at: str | None = None
    created_at: str | None = None


class TopicDetailOut(BaseModel):
    topic: TopicOut
    entries: list[EntryOut]


class EntryListOut(BaseModel):
    data: list[EntryOut]
    total: int


class EntryDetailOut(BaseModel):
    entry: EntryOut
    #: 演变过程：取代链上的全部条目（按编号）
    chain: list[EntryOut]


class TopicPatch(BaseModel):
    description: str | None = None
    resident: bool | None = None


class EntryCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1)
    #: 主题不存在时新建它：必填描述
    topic_description: str | None = None
    resident: bool = False


class EntryUpdate(BaseModel):
    """更正：用新内容取代这一条（来源记为用户亲自，补记不会改动它）。"""

    content: str = Field(min_length=1)
    #: 换到哪个主题；不给 = 原主题
    topic: str | None = None


class EntryRestoreIn(BaseModel):
    topic: str | None = None


class MoveIn(BaseModel):
    entries: list[int] = Field(min_length=1)
    to_topic: str


class WriteOut(BaseModel):
    number: int | None
    revision: str | None
    removed: list[int]


class PurgeOut(BaseModel):
    purged: list[int]


class RevisionOut(BaseModel):
    id: str
    kind: str
    topic: str | None
    removed: list[int]
    added: list[int]
    occurred_at: str
    source: str | None
    origin: dict[str, Any] | None = None
    undoes: str | None = None
    undone_by: str | None = None
    created_at: str


class RevisionListOut(BaseModel):
    data: list[RevisionOut]


class MemorySettingsOut(BaseModel):
    #: 暂停：两条写入路径都停止写入；已有记忆照常常驻、照常可以加载
    paused: bool
    enabled: bool


class MemorySettingsIn(BaseModel):
    paused: bool


class BackfillOut(BaseModel):
    id: UUID
    thread_id: UUID
    created_at: str
    seen_at: str | None
    #: [{scope, topic, number, content, revision}]
    written: list[dict[str, Any]]
    #: [{scope, topic, into}]
    split: list[dict[str, Any]]


class BackfillListOut(BaseModel):
    data: list[BackfillOut]


class BackfillSeenIn(BaseModel):
    #: 不给 = 全部标为已看
    ids: list[UUID] | None = None
