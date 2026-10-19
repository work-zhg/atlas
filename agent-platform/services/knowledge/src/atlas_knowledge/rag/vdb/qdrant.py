"""Qdrant 实现（doc/atlas-rag-design.html §8.2）。

collection   kb_{dataset_uuid_hex}
向量         dense（cosine）+ 稀疏 bm25（IDF modifier）—— 同一个 point，混合检索不用两个存储
payload 索引 document_uuid、segment_uuid、enabled、doc_ok、kind、acl、is_current、
             valid_from、valid_to；meta.* 按知识库的元数据字段定义另建（ensure_index）
"""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient, models

from .base import BaseVector, Hit, VectorPoint

__all__ = ["QdrantVector"]

DENSE = "dense"
SPARSE = "bm25"


_INDEX_TYPES = {
    "keyword": models.PayloadSchemaType.KEYWORD,
    "float": models.PayloadSchemaType.FLOAT,
    "bool": models.PayloadSchemaType.BOOL,
    "datetime": models.PayloadSchemaType.DATETIME,
}
_BASE_INDEXES = (
    ("document_uuid", "keyword"),
    ("segment_uuid", "keyword"),
    ("enabled", "bool"),
    ("doc_ok", "bool"),
    ("kind", "keyword"),
    ("acl", "keyword"),
    ("is_current", "bool"),
    ("valid_from", "float"),
    ("valid_to", "float"),
)


def _flags_filter(document_uuids: list[str] | None, conditions: Any = None) -> models.Filter:
    """conditions：检索层拼好的附加过滤（qdrant Filter：权限、版本时效、元数据），
    与开关过滤取与。"""
    must: list[Any] = [
        models.FieldCondition(key="enabled", match=models.MatchValue(value=True)),
        models.FieldCondition(key="doc_ok", match=models.MatchValue(value=True)),
    ]
    if document_uuids is not None:
        must.append(
            models.FieldCondition(key="document_uuid", match=models.MatchAny(any=document_uuids))
        )
    if conditions is not None:
        must.append(conditions)
    return models.Filter(must=must)


def _doc_filter(document_uuid: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="document_uuid", match=models.MatchValue(value=document_uuid))
        ]
    )


class QdrantVector(BaseVector):
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        super().__init__(collection)
        self._c = client

    async def create(self, dim: int) -> None:
        if await self._c.collection_exists(self.collection):
            return
        await self._c.create_collection(
            self.collection,
            vectors_config={DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        for key, kind in _BASE_INDEXES:
            await self.ensure_index(key, kind)

    async def ensure_index(self, key: str, kind: str) -> None:
        if not await self._c.collection_exists(self.collection):
            return
        info = await self._c.get_collection(self.collection)
        if key in (info.payload_schema or {}):
            return
        await self._c.create_payload_index(
            self.collection, key, field_schema=_INDEX_TYPES[kind], wait=True
        )

    async def add(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        await self._c.upsert(
            self.collection,
            points=[
                models.PointStruct(
                    id=p.id,
                    vector={
                        DENSE: p.dense,
                        SPARSE: models.SparseVector(
                            indices=p.sparse_indices, values=p.sparse_values
                        ),
                    },
                    payload=p.payload,
                )
                for p in points
            ],
            wait=True,
        )

    async def delete_by_ids(self, ids: list[str]) -> None:
        if ids and await self._c.collection_exists(self.collection):
            await self._c.delete(
                self.collection, points_selector=models.PointIdsList(points=ids), wait=True
            )

    async def delete_by_document(self, document_uuid: str) -> None:
        if await self._c.collection_exists(self.collection):
            await self._c.delete(
                self.collection,
                points_selector=models.FilterSelector(filter=_doc_filter(document_uuid)),
                wait=True,
            )

    async def delete(self) -> None:
        if await self._c.collection_exists(self.collection):
            await self._c.delete_collection(self.collection)

    async def set_enabled(self, ids: list[str], enabled: bool) -> None:
        if ids:
            await self._c.set_payload(
                self.collection, payload={"enabled": enabled}, points=ids, wait=True
            )

    async def set_doc_ok(self, document_uuid: str, ok: bool) -> None:
        if await self._c.collection_exists(self.collection):
            await self._c.set_payload(
                self.collection,
                payload={"doc_ok": ok},
                points=_doc_filter(document_uuid),
                wait=True,
            )

    async def set_doc_payload(self, document_uuid: str, payload: dict[str, Any]) -> None:
        if not payload or not await self._c.collection_exists(self.collection):
            return
        keep = {k: v for k, v in payload.items() if v is not None}
        if keep:
            await self._c.set_payload(
                self.collection, payload=keep, points=_doc_filter(document_uuid), wait=True
            )
        # 值为 None 的键（如取消生效区间）删掉：检索按「没有这个键」处理
        gone = [k for k, v in payload.items() if v is None]
        if gone:
            await self._c.delete_payload(
                self.collection, keys=gone, points=_doc_filter(document_uuid), wait=True
            )

    async def set_points_payload(self, ids: list[str], payload: dict[str, Any]) -> None:
        if ids and payload and await self._c.collection_exists(self.collection):
            await self._c.set_payload(self.collection, payload=payload, points=ids, wait=True)

    async def point_ids(self) -> list[str]:
        if not await self._c.collection_exists(self.collection):
            return []
        out: list[str] = []
        offset = None
        while True:
            rows, offset = await self._c.scroll(
                self.collection, limit=1000, offset=offset, with_payload=False, with_vectors=False
            )
            out.extend(str(r.id) for r in rows)
            if offset is None:
                return out

    async def search_by_vector(
        self,
        vector: list[float],
        *,
        top_k: int,
        score_threshold: float | None = None,
        document_uuids: list[str] | None = None,
        conditions: Any = None,
    ) -> list[Hit]:
        if not await self._c.collection_exists(self.collection):
            return []
        res = await self._c.query_points(
            self.collection,
            query=vector,
            using=DENSE,
            limit=top_k,
            score_threshold=score_threshold,
            query_filter=_flags_filter(document_uuids, conditions),
            with_payload=True,
            with_vectors=[DENSE],
        )
        return [
            Hit(
                id=str(p.id),
                score=float(p.score),
                payload=p.payload or {},
                vector=(p.vector or {}).get(DENSE) if isinstance(p.vector, dict) else None,  # type: ignore[arg-type]
            )
            for p in res.points
        ]

    async def search_by_full_text(
        self,
        indices: list[int],
        values: list[float],
        *,
        top_k: int,
        document_uuids: list[str] | None = None,
        conditions: Any = None,
    ) -> list[Hit]:
        if not indices or not await self._c.collection_exists(self.collection):
            return []
        res = await self._c.query_points(
            self.collection,
            query=models.SparseVector(indices=indices, values=values),
            using=SPARSE,
            limit=top_k,
            query_filter=_flags_filter(document_uuids, conditions),
            with_payload=True,
            with_vectors=[DENSE],
        )
        return [
            Hit(
                id=str(p.id),
                score=float(p.score),
                payload=p.payload or {},
                vector=(p.vector or {}).get(DENSE) if isinstance(p.vector, dict) else None,  # type: ignore[arg-type]
            )
            for p in res.points
        ]

    async def vectors(self, ids: list[str]) -> dict[str, list[float]]:
        if not ids:
            return {}
        rows = await self._c.retrieve(
            self.collection, ids=ids, with_vectors=[DENSE], with_payload=False
        )
        out: dict[str, list[float]] = {}
        for r in rows:
            if isinstance(r.vector, dict) and DENSE in r.vector:
                out[str(r.id)] = list(r.vector[DENSE])  # type: ignore[arg-type]
        return out
