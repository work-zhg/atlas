"""RAG 流水线里流转的文档对象（同 Dify core/rag/models/document.py）。

解析器产出它、分段器切它、索引处理器给它挂子块、检索返回它。与库表无关。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Document", "text_hash"]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Document:
    page_content: str
    #: doc_id（分段 / 子块 uuid）、doc_hash、document_uuid、dataset_uuid、score …
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 父子模式：子块
    children: list[Document] | None = None
    #: 检索时带回的向量（加权重排用）
    vector: list[float] | None = None
