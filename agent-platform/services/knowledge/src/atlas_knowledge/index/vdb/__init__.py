"""向量库工厂（同 Dify vdb/vector_factory.py）：按知识库取对应实现。P1 只有 Qdrant。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .base import BaseVector, Hit, VectorPoint
from .qdrant import QdrantVector

__all__ = ["BaseVector", "Hit", "VectorFactory", "VectorPoint", "VectorType", "collection_name"]


class VectorType(StrEnum):
    QDRANT = "qdrant"


def collection_name(dataset_uuid: Any) -> str:
    return f"kb_{str(dataset_uuid).replace('-', '')}"


class VectorFactory:
    def __init__(self, client: Any, vector_type: VectorType = VectorType.QDRANT) -> None:
        self._client = client
        self._type = vector_type

    def for_collection(self, collection: str) -> BaseVector:
        if self._type is VectorType.QDRANT:
            return QdrantVector(self._client, collection)
        raise ValueError(f"不支持的向量库：{self._type}")
