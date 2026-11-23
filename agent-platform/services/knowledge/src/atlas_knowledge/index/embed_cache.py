"""带缓存的向量化（参考 Dify core/rag/embedding/cached_embedding.py）。

文档向量查 kb_embedding_cache（模型 + 文本哈希），查询向量缓存在 Redis。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import EmbeddingCache
from ..models.embedding import EmbeddingModel
from ..ragdoc import text_hash

__all__ = ["CacheEmbedding"]

logger = logging.getLogger(__name__)


def _record_usage(tokens: int, *, cached: int | None = None) -> None:
    """向量化用量写进当前 span（入库的 load、检索的 embed_query）：累加，同一 span 多批次也对。"""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    attrs = getattr(span, "attributes", None) or {}
    if tokens:
        prev = int(attrs.get("knowledge.embedding.tokens") or 0)
        span.set_attribute("knowledge.embedding.tokens", prev + tokens)
    if cached is not None:
        prev = int(attrs.get("knowledge.embedding.cached") or 0)
        span.set_attribute("knowledge.embedding.cached", prev + cached)


def _pack(vec: list[float]) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def _unpack(raw: bytes) -> list[float]:
    return [float(x) for x in np.frombuffer(raw, dtype="<f4")]


class CacheEmbedding:
    """带缓存的向量化。文档向量落库（重建索引时复用），查询向量放 Redis（短期）。"""

    def __init__(self, model: EmbeddingModel, *, redis: Any = None, query_ttl_s: int = 600) -> None:
        self._model = model
        self._redis = redis
        self._ttl = query_ttl_s

    @property
    def model(self) -> EmbeddingModel:
        return self._model

    async def embed_documents(
        self, session: AsyncSession, texts: list[str], *, batch: int = 32
    ) -> list[list[float]]:
        hashes = [text_hash(t) for t in texts]
        found: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(hashes))
        for i in range(0, len(unique), 500):
            rows = await session.execute(
                select(EmbeddingCache.text_hash, EmbeddingCache.embedding).where(
                    EmbeddingCache.provider == self._model.provider,
                    EmbeddingCache.model == self._model.model,
                    EmbeddingCache.text_hash.in_(unique[i : i + 500]),
                )
            )
            found.update({h: _unpack(raw) for h, raw in rows.all()})

        missing = [
            (h, t) for h, t in dict(zip(hashes, texts, strict=True)).items() if h not in found
        ]
        tokens = 0
        for i in range(0, len(missing), batch):
            part = missing[i : i + batch]
            vectors, used = await asyncio.to_thread(self._embed, [t for _, t in part])
            tokens += used
            for (h, _), vec in zip(part, vectors, strict=True):
                found[h] = vec
            await session.execute(
                pg_insert(EmbeddingCache)
                .values(
                    [
                        {
                            "provider": self._model.provider,
                            "model": self._model.model,
                            "text_hash": h,
                            "embedding": _pack(found[h]),
                        }
                        for h, _ in part
                    ]
                )
                .on_conflict_do_nothing(index_elements=["provider", "model", "text_hash"])
            )
        _record_usage(tokens, cached=len(unique) - len(missing))
        return [found[h] for h in hashes]

    def _embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """远程模型带用量；本地模型用量记 0。"""
        with_usage = getattr(self._model, "embed_with_usage", None)
        if with_usage is not None:
            return with_usage(texts)
        return self._model.embed_documents(texts), 0

    async def embed_query(self, text: str) -> list[float]:
        key = f"kb:qemb:{self._model.provider}:{self._model.model}:{text_hash(text)}"
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                if raw:
                    await self._redis.expire(key, self._ttl)
                    trace.get_current_span().set_attribute("knowledge.embedding.cache_hit", True)
                    return _unpack(raw)
            except Exception:  # 缓存不可用不影响检索
                logger.warning("查询向量缓存读取失败", exc_info=True)
        if getattr(self._model, "embed_with_usage", None) is not None:
            vecs, used = await asyncio.to_thread(self._model.embed_with_usage, [text])  # type: ignore[attr-defined]
            vec = vecs[0]
            _record_usage(used)
        else:  # 本地模型的查询向量有专门的前缀处理（bge 的 query instruction）
            vec = await asyncio.to_thread(self._model.embed_query, text)
        if self._redis is not None:
            try:
                await self._redis.set(key, _pack(vec), ex=self._ttl)
            except Exception:
                logger.warning("查询向量缓存写入失败", exc_info=True)
        return vec
