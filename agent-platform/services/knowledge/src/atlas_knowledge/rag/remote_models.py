"""远程向量与重排模型（doc/knowledge-enterprise-design.html §6.1）。

    OpenAICompatibleEmbedding   POST {base}/embeddings（模型网关、TEI、vLLM、云厂商都兼容）
    RemoteReranker              cohere_compatible：POST {base}/rerank {model, query, documents}
                                                   → {results: [{index, relevance_score}]}
                                                   （Cohere / Jina / SiliconFlow / Xinference）
                                tei：POST {base}/rerank {query, texts} → [{index, score}]

★ 同步接口（与 fastembed 一致，调用方放线程里跑）；批量由调用方控制（CacheEmbedding 按批调用）。
★ 429 / 5xx / 超时按指数退避重试；维度在第一次加载时探测，知识库记录的维度对不上直接报错。
★ token 用量（usage.total_tokens）：embed_with_usage 返回本次用量，CacheEmbedding 写进当前 span
  （knowledge.embedding.tokens）；tokens 是进程内累计。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

__all__ = ["OpenAICompatibleEmbedding", "RemoteReranker"]

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _post(client: httpx.Client, path: str, body: dict[str, Any], retries: int) -> Any:
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            r = client.post(path, json=body)
        except httpx.TransportError:
            if attempt == retries:
                raise
        else:
            if r.status_code < 400:
                return r.json()
            if r.status_code not in _RETRY_STATUS or attempt == retries:
                r.raise_for_status()
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def _client(
    base_url: str,
    api_key: str | None,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    """headers：经模型网关时的平台用途（X-Model-Purpose、X-Caller-App），不带 Key。"""
    headers = {**({"Authorization": f"Bearer {api_key}"} if api_key else {}), **(headers or {})}
    return httpx.Client(
        base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s, transport=transport
    )


class OpenAICompatibleEmbedding:
    provider = "openai_compatible"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self._retries = max_retries
        self._c = _client(base_url, api_key, timeout_s, transport, headers)
        self._lock = threading.Lock()
        self.tokens = 0
        self.dim = len(self.embed_query("dim"))

    def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0
        data = _post(self._c, "/embeddings", {"model": self.model, "input": texts}, self._retries)
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        if len(rows) != len(texts):
            raise RuntimeError(f"向量接口返回 {len(rows)} 条，请求 {len(texts)} 条")
        used = int((data.get("usage") or {}).get("total_tokens") or 0)
        with self._lock:
            self.tokens += used
        return [[float(x) for x in d["embedding"]] for d in rows], used

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_with_usage(texts)[0]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_with_usage([text])[0][0]


class RemoteReranker:
    def __init__(
        self,
        model: str,
        *,
        provider: str,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if provider not in ("cohere_compatible", "tei"):
            raise ValueError(f"不支持的重排提供方：{provider}")
        self.model = model
        self.provider = provider
        self._retries = max_retries
        self._c = _client(base_url, api_key, timeout_s, transport, headers)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        scores = [0.0] * len(texts)
        if self.provider == "tei":
            body: dict[str, Any] = {"query": query, "texts": texts, "raw_scores": False}
            for item in _post(self._c, "/rerank", body, self._retries):
                scores[int(item["index"])] = float(item["score"])
        else:
            body = {
                "model": self.model,
                "query": query,
                "documents": texts,
                "top_n": len(texts),
                "return_documents": False,
            }
            data = _post(self._c, "/rerank", body, self._retries)
            for item in data.get("results") or []:
                scores[int(item["index"])] = float(item["relevance_score"])
        return scores
