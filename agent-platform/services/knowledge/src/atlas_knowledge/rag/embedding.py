"""向量模型与缓存（参考 Dify core/rag/embedding/cached_embedding.py）。

    EmbeddingModel   同步接口（CPU 推理），由调用方放线程里跑
    FastEmbedModel   fastembed 本地 ONNX（开发与小规模）
    远程提供方        openai_compatible 向量、cohere_compatible / tei 重排（remote_models.py）
    CacheEmbedding   文档向量查 kb_embedding_cache（模型 + 文本哈希），查询向量缓存在 Redis

★ 模型对象进程级缓存：加载一次要几百毫秒到几秒，且占内存。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Protocol

import numpy as np
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import EmbeddingCache
from ..errors import Invalid
from .models import text_hash

__all__ = [
    "CacheEmbedding",
    "EmbeddingModel",
    "FastEmbedModel",
    "ModelRegistry",
    "Reranker",
]

logger = logging.getLogger(__name__)


class EmbeddingModel(Protocol):
    provider: str
    model: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    model: str

    def rerank(self, query: str, texts: list[str]) -> list[float]: ...


def _to_list(vec: Any) -> list[float]:
    return [float(x) for x in np.asarray(vec, dtype=np.float32).tolist()]


class FastEmbedModel:
    provider = "fastembed"

    def __init__(self, model: str, cache_dir: str | None) -> None:
        from fastembed import TextEmbedding

        self.model = model
        self._impl = TextEmbedding(model_name=model, cache_dir=cache_dir)
        self.dim = len(next(iter(self._impl.embed(["dim"]))))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_to_list(v) for v in self._impl.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return _to_list(next(iter(self._impl.query_embed(text))))


class FastEmbedReranker:
    def __init__(self, model: str, cache_dir: str | None) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.model = model
        self._impl = TextCrossEncoder(model_name=model, cache_dir=cache_dir)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        # 交叉编码输出 logit，sigmoid 归一到 0~1，才能和分数阈值比较
        scores = np.asarray(list(self._impl.rerank(query, texts)), dtype=np.float64)
        return [float(x) for x in 1.0 / (1.0 + np.exp(-scores))]


class ModelRegistry:
    """按 (provider, model) 取模型；测试注入假实现。"""

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        remote: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """remote：{"embedding": {base_url, api_key, timeout_s}, "rerank": {...}}，没配就不支持。"""
        self._cache_dir = cache_dir
        self._remote = remote or {}
        self._embeddings: dict[tuple[str, str], EmbeddingModel] = {}
        self._rerankers: dict[tuple[str, str], Reranker] = {}
        self._lock = threading.Lock()

    def embedding(self, provider: str, model: str) -> EmbeddingModel:
        key = (provider, model)
        with self._lock:
            hit = self._embeddings.get(key)
            if hit is not None:
                return hit
            logger.info("加载向量模型 %s/%s", provider, model)
            impl: EmbeddingModel
            if provider == "fastembed":
                impl = FastEmbedModel(model, self._cache_dir)
            elif provider == "openai_compatible":
                from .remote_models import OpenAICompatibleEmbedding

                impl = OpenAICompatibleEmbedding(model, **self._remote_cfg("embedding"))
            else:
                raise Invalid(f"不支持的向量模型提供方：{provider}")
            self._embeddings[key] = impl
            return impl

    def reranker(self, model: str, provider: str = "fastembed") -> Reranker:
        key = (provider, model)
        with self._lock:
            hit = self._rerankers.get(key)
            if hit is not None:
                return hit
            impl: Reranker
            if provider == "fastembed":
                logger.info("加载重排模型 %s（首次使用会下载）", model)
                impl = FastEmbedReranker(model, self._cache_dir)
            elif provider in ("cohere_compatible", "tei"):
                from .remote_models import RemoteReranker

                impl = RemoteReranker(model, provider=provider, **self._remote_cfg("rerank"))
            else:
                raise Invalid(f"不支持的重排提供方：{provider}")
            self._rerankers[key] = impl
            return impl

    def _remote_cfg(self, kind: str) -> dict[str, Any]:
        cfg = self._remote.get(kind) or {}
        if not cfg.get("base_url"):
            name = (
                "ATLAS_KB_EMBEDDING_BASE_URL" if kind == "embedding" else "ATLAS_KB_RERANK_BASE_URL"
            )
            raise Invalid(f"远程模型地址未配置（{name}）")
        return cfg

    def register_embedding(self, impl: EmbeddingModel) -> None:
        self._embeddings[(impl.provider, impl.model)] = impl

    def register_reranker(self, impl: Reranker) -> None:
        self._rerankers[(getattr(impl, "provider", "fastembed"), impl.model)] = impl


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
