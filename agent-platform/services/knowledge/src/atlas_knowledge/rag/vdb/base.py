"""向量库抽象（同 Dify core/rag/datasource/vdb/vector_base.py 的 BaseVector）。

一个实例对应一个知识库的 collection。上层（入库、检索）只认这个接口，换向量库不动上层。

point 的 payload 约定：
    kind           segment | child | summary（检索入口类型，§5.5）
    segment_uuid   命中后回到哪个分段（父子模式下是父分段）
    document_uuid / dataset_uuid
    enabled        分段级开关
    doc_ok         文档级可用（启用、未归档、审核通过）—— 文档状态变化时按 document_uuid 批量改
    meta           文档业务元数据 {字段名: 值}（§7.2）
    acl            可访问主体标签（§7.4）；["public"] = 库内公开
    is_current     是否当前版本；valid_from / valid_to 生效区间（epoch 秒，没有就不写）（§7.5）
检索永远带 enabled = doc_ok = true 过滤。文档级字段（doc_ok、meta、acl、版本时效）变化时
只按 document_uuid 改 payload，不重新向量化。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = ["BaseVector", "Hit", "VectorPoint"]


@dataclass
class VectorPoint:
    id: str
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]
    vector: list[float] | None = None


class BaseVector(ABC):
    def __init__(self, collection: str) -> None:
        self.collection = collection

    @abstractmethod
    async def create(self, dim: int) -> None:
        """建 collection（已存在则跳过）。"""

    @abstractmethod
    async def add(self, points: list[VectorPoint]) -> None:
        """写入或覆盖（point id 相同即覆盖）。"""

    @abstractmethod
    async def delete_by_ids(self, ids: list[str]) -> None: ...

    @abstractmethod
    async def delete_by_document(self, document_uuid: str) -> None: ...

    @abstractmethod
    async def delete(self) -> None:
        """删整个 collection。"""

    @abstractmethod
    async def set_enabled(self, ids: list[str], enabled: bool) -> None: ...

    @abstractmethod
    async def set_doc_ok(self, document_uuid: str, ok: bool) -> None: ...

    @abstractmethod
    async def set_doc_payload(self, document_uuid: str, payload: dict[str, Any]) -> None:
        """按文档覆盖若干 payload 键（元数据、权限、版本时效变更）。"""

    @abstractmethod
    async def set_points_payload(self, ids: list[str], payload: dict[str, Any]) -> None:
        """按 point 覆盖若干 payload 键（分段级元数据覆盖）。"""

    @abstractmethod
    async def ensure_index(self, key: str, kind: str) -> None:
        """建 payload 索引（kind：keyword | float | bool | datetime）；已存在则跳过。"""

    @abstractmethod
    async def point_ids(self) -> list[str]:
        """collection 里所有 point 的 id（重建索引后清理孤儿 point 用）。"""

    @abstractmethod
    async def search_by_vector(
        self,
        vector: list[float],
        *,
        top_k: int,
        score_threshold: float | None = None,
        document_uuids: list[str] | None = None,
        conditions: Any = None,
    ) -> list[Hit]: ...

    @abstractmethod
    async def search_by_full_text(
        self,
        indices: list[int],
        values: list[float],
        *,
        top_k: int,
        document_uuids: list[str] | None = None,
        conditions: Any = None,
    ) -> list[Hit]: ...

    @abstractmethod
    async def vectors(self, ids: list[str]) -> dict[str, list[float]]:
        """取 point 的稠密向量（加权重排给全文命中补算余弦用）。"""
