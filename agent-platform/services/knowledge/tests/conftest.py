"""知识库服务测试的夹具。

★ 独立测试库 atlas_knowledge_test，不碰开发库（运行时那边的 clean_db 清掉过开发数据）。
★ Redis 用 12 号库，只清 kb: 前缀的键；Qdrant 用 qdrant-client 的内存模式；
  向量模型用确定性的假实现（字符 bigram 哈希），不下载模型；对象存储用内存字典。
★ 投递器只记录要入库的文档；测试显式调 run_indexing() 跑 runner —— 与 worker 执行的是同一段代码。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import numpy as np
import pytest
from atlas_knowledge.db.session import get_engine, get_sessionmaker
from atlas_knowledge.settings import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB = "atlas_knowledge_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
TEST_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{TEST_DB}"
TEST_REDIS = "redis://localhost:6380/12"
_TABLES = (
    "kb_dataset",
    "kb_dataset_permission",
    "kb_process_rule",
    "kb_document",
    "kb_segment",
    "kb_child_chunk",
    "kb_embedding_cache",
    "kb_query_log",
    "kb_retrieval",
)


@pytest.fixture(scope="session", autouse=True)
def _test_env() -> Iterator[None]:
    saved = {
        k: os.environ.get(k)
        for k in ("ATLAS_KB_DATABASE_URL", "ATLAS_KB_REDIS_URL", "ATLAS_KB_AUTH_MODE")
    }
    os.environ["ATLAS_KB_DATABASE_URL"] = TEST_URL
    os.environ["ATLAS_KB_REDIS_URL"] = TEST_REDIS
    os.environ["ATLAS_KB_AUTH_MODE"] = "dev"
    get_settings.cache_clear()

    async def _ensure() -> None:
        engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}
            )
            if not exists:
                await conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
        await engine.dispose()

    asyncio.run(_ensure())
    from atlas_knowledge.__main__ import migrate

    migrate()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


class FakeEmbedding:
    """确定性的假向量：字符 bigram 哈希到 256 维再归一化。字面越像，余弦越高。"""

    provider = "fastembed"
    model = "fake-embedding"
    dim = 256

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self.dim, dtype=np.float64)
        s = text.lower()
        for i in range(len(s) - 1):
            h = int.from_bytes(hashlib.md5(s[i : i + 2].encode()).digest()[:4], "little")
            v[h % self.dim] += 1.0
        n = np.linalg.norm(v)
        return (v / n if n else v).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class FakeReranker:
    """按查询与候选的公共字符数打分（够判断顺序就行）。"""

    model = "fake-reranker"

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        q = set(query)
        return [len(q & set(t)) / (len(q) or 1) for t in texts]


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            del self.objects[k]
        return len(keys)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.queued: list[str] = []

    def indexing(self, document_uuids: list[str]) -> None:
        self.queued.extend(document_uuids)


@pytest.fixture
async def rt() -> AsyncIterator[Any]:
    import redis.asyncio as aioredis
    from atlas_knowledge.rag.embedding import ModelRegistry
    from atlas_knowledge.rag.vdb import VectorFactory
    from atlas_knowledge.runtime import Runtime
    from qdrant_client import AsyncQdrantClient

    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {', '.join(_TABLES)}"))
        await session.commit()

    redis = aioredis.from_url(TEST_REDIS)
    async for key in redis.scan_iter("kb:*"):
        await redis.delete(key)

    registry = ModelRegistry()
    registry.register_embedding(FakeEmbedding())
    registry.register_reranker(FakeReranker())
    storage = MemoryStorage()
    runtime = Runtime(
        settings=get_settings(),
        registry=registry,
        vectors=VectorFactory(AsyncQdrantClient(location=":memory:")),
        redis=redis,
        dispatcher=RecordingDispatcher(),
        storage_factory=lambda: storage,
    )
    try:
        yield runtime
    finally:
        await runtime.aclose()
        # 引擎绑定 event loop；每个测试一个 loop，所以每个测试结束都要重建（同运行时 conftest）
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


@pytest.fixture
async def client(rt: Any) -> AsyncIterator[Any]:
    import httpx
    from atlas_knowledge.api.app import create_app

    app = create_app(runtime=rt)
    app.state.runtime = rt
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://kb") as c:
        yield c


async def run_indexing(rt: Any) -> None:
    """把投递器记下的文档跑一遍（worker 跑的就是这段）。"""
    from atlas_knowledge.indexing.runner import IndexingRunner

    runner = IndexingRunner(rt, get_sessionmaker())
    queued, rt.dispatcher.queued = list(rt.dispatcher.queued), []
    for doc_uuid in queued:
        await runner.run(doc_uuid)
