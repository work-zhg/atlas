"""知识库的生命周期事件（c4-model/knowledge/code-dataset.html · DatasetEvents）。

    DatasetCreated         向量索引登记第 1 个索引版本
    DatasetDeleted         文档与入库、分段、检索、向量索引各自删掉自己的数据
    MetadataFieldDefined   向量索引给每个 collection 的 meta 建这个字段的路径索引

★ 在同一个事务里同步分发：订阅方拿着发布方的 session 处理自己的数据，任何一方失败整个事务回滚。
  不引入消息队列。
★ 订阅关系在组合根登记（wiring.py）：知识库领域只发事件，不认识订阅方。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Dataset
from ..retrieval.filters import FieldDef

if TYPE_CHECKING:
    from ..runtime import Runtime

__all__ = [
    "DatasetCreated",
    "DatasetDeleted",
    "DatasetEvents",
    "MetadataFieldDefined",
]


@dataclass(frozen=True)
class DatasetCreated:
    dataset: Dataset
    #: 建库时选的向量模型（已加载过，维度是探测出来的）
    embedding_provider: str
    embedding_model: str
    embedding_dim: int
    actor: str


@dataclass(frozen=True)
class DatasetDeleted:
    """库这一行还在时发：订阅方可能要用它（如文件目录 storage_prefix）。"""

    dataset: Dataset


@dataclass(frozen=True)
class MetadataFieldDefined:
    dataset_uuid: UUID
    field: FieldDef


Handler = Callable[[AsyncSession, "Runtime", Any], Awaitable[None]]


class DatasetEvents:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, session: AsyncSession, rt: Runtime, event: Any) -> None:
        """按登记的顺序逐个调用；调用方负责提交事务。"""
        for handler in self._handlers[type(event)]:
            await handler(session, rt, event)
