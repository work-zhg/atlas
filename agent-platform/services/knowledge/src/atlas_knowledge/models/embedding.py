"""向量与重排模型。

    EmbeddingModel   同步接口（CPU 推理），由调用方放线程里跑
    FastEmbedModel   fastembed 本地 ONNX（开发与小规模）
    远程提供方        openai_compatible 向量、cohere_compatible / tei 重排（remote_models.py）
    带缓存的向量化    CacheEmbedding（index/embed_cache.py）

★ 模型对象进程级缓存：加载一次要几百毫秒到几秒，且占内存。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

import numpy as np

from ..errors import Invalid

__all__ = [
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
            client = (
                "ATLAS_KB_EMBEDDING_CLIENT" if kind == "embedding" else "ATLAS_KB_RERANK_CLIENT"
            )
            raise Invalid(f"远程模型要经模型平台（ATLAS_KB_MODEL_PLATFORM_URL 与 {client}）")
        return cfg

    def register_embedding(self, impl: EmbeddingModel) -> None:
        self._embeddings[(impl.provider, impl.model)] = impl

    def register_reranker(self, impl: Reranker) -> None:
        self._rerankers[(getattr(impl, "provider", "fastembed"), impl.model)] = impl
