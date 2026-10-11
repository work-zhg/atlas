"""Qdrant 实现（doc/atlas-rag-design.html §8.2）。

collection   kb_{dataset_uuid_hex}
向量         dense（cosine）+ 稀疏 bm25（IDF modifier）—— 同一个 point，混合检索不用两个存储
payload 索引 document_uuid、segment_uuid、enabled、doc_ok
"""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient, models

from .base import BaseVector, Hit, VectorPoint

__all__ = ["QdrantVector"]

DENSE = "dense"
SPARSE = "bm25"


def _flags_filter(document_uuids: list[str] | None) -> models.Filter:
    must: list[Any] = [
        models.FieldCondition(key="enabled", match=models.MatchValue(value=True)),
        models.FieldCondition(key="doc_ok", match=models.MatchValue(value=True)),
    ]
    if document_uuids is not None:
        must.append(
            models.FieldCondition(key="document_uuid", match=models.MatchAny(any=document_uuids))
        )
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
        for key, schema in (
            ("document_uuid", models.PayloadSchemaType.KEYWORD),
            ("segment_uuid", models.PayloadSchemaType.KEYWORD),
            ("enabled", models.PayloadSchemaType.BOOL),
            ("doc_ok", models.PayloadSchemaType.BOOL),
        ):
            await self._c.create_payload_index(self.collection, key, field_schema=schema)

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

    async def search_by_vector(
        self,
        vector: list[float],
        *,
        top_k: int,
        score_threshold: float | None = None,
        document_uuids: list[str] | None = None,
    ) -> list[Hit]:
        if not await self._c.collection_exists(self.collection):
            return []
        res = await self._c.query_points(
            self.collection,
            query=vector,
            using=DENSE,
            limit=top_k,
            score_threshold=score_threshold,
            query_filter=_flags_filter(document_uuids),
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
    ) -> list[Hit]:
        if not indices or not await self._c.collection_exists(self.collection):
            return []
        res = await self._c.query_points(
            self.collection,
            query=models.SparseVector(indices=indices, values=values),
            using=SPARSE,
            limit=top_k,
            query_filter=_flags_filter(document_uuids),
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
