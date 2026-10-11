"""进程级依赖：模型、向量库、Redis、对象存储、任务投递。api 与 worker 各建一份。

测试用 Runtime(...) 直接注入假实现（内存 Qdrant、假向量模型、内联投递）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .rag.embedding import ModelRegistry
from .rag.vdb import VectorFactory
from .settings import KnowledgeSettings
from .storage import FileStorage, make_storage

__all__ = ["Dispatcher", "Runtime", "build_runtime"]

logger = logging.getLogger(__name__)


class Dispatcher(Protocol):
    """异步任务投递。生产是 Celery；测试记下来或内联执行。"""

    def indexing(self, document_uuids: list[str]) -> None: ...


class CeleryDispatcher:
    def indexing(self, document_uuids: list[str]) -> None:
        from .tasks.indexing import indexing_task

        indexing_task.delay(document_uuids)


@dataclass
class Runtime:
    settings: KnowledgeSettings
    registry: ModelRegistry
    vectors: VectorFactory
    redis: Any
    dispatcher: Dispatcher
    storage_factory: Callable[[], FileStorage] | None = None
    _storage: FileStorage | None = field(default=None, repr=False)

    @property
    def storage(self) -> FileStorage:
        if self._storage is None:
            factory = self.storage_factory or (lambda: make_storage(self.settings))
            self._storage = factory()
        return self._storage

    async def aclose(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
        client = getattr(self.vectors, "_client", None)
        if client is not None:
            await client.close()


def build_runtime(settings: KnowledgeSettings, *, dispatcher: Dispatcher | None = None) -> Runtime:
    import redis.asyncio as aioredis
    from qdrant_client import AsyncQdrantClient

    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        timeout=30,
    )
    return Runtime(
        settings=settings,
        registry=ModelRegistry(cache_dir=settings.model_cache_dir),
        vectors=VectorFactory(qdrant),
        redis=aioredis.from_url(settings.redis_url),
        dispatcher=dispatcher or CeleryDispatcher(),
    )
