"""进程级依赖：模型、向量库、Redis、对象存储、任务投递。api 与 worker 各建一份。

测试用 Runtime(...) 直接注入假实现（内存 Qdrant、假向量模型、内联投递）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .perms import DataPermissions, make_data_permissions
from .principals import PrincipalResolver, make_resolver
from .rag.embedding import ModelRegistry
from .rag.llm import ChatModel, OpenAICompatibleChat
from .rag.model_gateway import PurposeCatalog, PurposeChat, purpose_headers
from .rag.vdb import VectorFactory
from .settings import KnowledgeSettings
from .storage import FileStorage, make_storage

__all__ = ["Dispatcher", "Runtime", "build_runtime"]

logger = logging.getLogger(__name__)


class Dispatcher(Protocol):
    """异步任务投递。生产是 Celery；测试记下来或内联执行。"""

    def indexing(self, document_uuids: list[str]) -> None: ...

    def rebuild_index(self, version_uuid: str) -> None: ...


class CeleryDispatcher:
    def indexing(self, document_uuids: list[str]) -> None:
        from .tasks.indexing import indexing_task

        indexing_task.delay(document_uuids)

    def rebuild_index(self, version_uuid: str) -> None:
        from .tasks.indexing import rebuild_index_task

        rebuild_index_task.delay(version_uuid)


@dataclass
class Runtime:
    settings: KnowledgeSettings
    registry: ModelRegistry
    vectors: VectorFactory
    redis: Any
    dispatcher: Dispatcher
    storage_factory: Callable[[], FileStorage] | None = None
    _storage: FileStorage | None = field(default=None, repr=False)
    #: 上下文补全、表格摘要、查询理解用的 LLM。None = 没配，相关增强降级为不做
    llm: ChatModel | None = None
    #: 查询理解单独用的 LLM（经模型网关时是平台用途 kb_query）。None = 和 llm 共用
    query_llm: ChatModel | None = None
    #: 模型网关的平台用途配置（建库时可选的远程嵌入、重排模型）。None = 没接模型网关
    model_purposes: PurposeCatalog | None = None
    #: 用户 → 主体标签（文档级权限）。None = 按 settings 建（dev：只有 u:用户）
    principal_resolver: PrincipalResolver | None = None
    #: 库级权限（用户中心数据权限）。None = 按 settings 建（perms.make_data_permissions）
    data_permissions: DataPermissions | None = None

    @property
    def perms(self) -> DataPermissions:
        if self.data_permissions is None:
            self.data_permissions = make_data_permissions(self.settings)
        return self.data_permissions

    async def principals(self, user_id: str) -> list[str]:
        if self.principal_resolver is None:
            self.principal_resolver = make_resolver(self.settings)
        return await self.principal_resolver(user_id)

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
        for llm in (self.llm, self.query_llm):
            if llm is not None and hasattr(llm, "aclose"):
                await llm.aclose()


def _remote(base_url: str | None, key: Any, timeout_s: float) -> dict[str, Any]:
    if not base_url:
        return {}
    return {
        "base_url": base_url,
        "api_key": key.get_secret_value() if key else None,
        "timeout_s": timeout_s,
    }


def build_runtime(settings: KnowledgeSettings, *, dispatcher: Dispatcher | None = None) -> Runtime:
    import redis.asyncio as aioredis
    from qdrant_client import AsyncQdrantClient

    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        timeout=30,
    )
    if settings.model_gateway_url:
        return _gateway_runtime(settings, qdrant, dispatcher)
    return Runtime(
        settings=settings,
        registry=ModelRegistry(
            cache_dir=settings.model_cache_dir,
            remote={
                "embedding": _remote(
                    settings.embedding_base_url,
                    settings.embedding_api_key,
                    settings.remote_model_timeout_s,
                ),
                "rerank": _remote(
                    settings.rerank_base_url,
                    settings.rerank_api_key,
                    settings.remote_model_timeout_s,
                ),
            },
        ),
        vectors=VectorFactory(qdrant),
        redis=aioredis.from_url(settings.redis_url),
        dispatcher=dispatcher or CeleryDispatcher(),
        llm=(
            OpenAICompatibleChat(
                settings.llm_base_url,
                settings.llm_api_key.get_secret_value() if settings.llm_api_key else "",
                settings.llm_model,
                timeout_s=settings.llm_timeout_s,
            )
            if settings.llm_base_url
            else None
        ),
    )


def _gateway_runtime(
    settings: KnowledgeSettings, qdrant: Any, dispatcher: Dispatcher | None
) -> Runtime:
    """经模型网关（doc/model-design.html §6.7）：全部按平台用途调。

    不写死模型名，也不持有供应商 Key。
    """
    import redis.asyncio as aioredis

    gateway = str(settings.model_gateway_url).rstrip("/")
    app = settings.uc_app_code
    purposes = PurposeCatalog(gateway, ttl_s=settings.model_catalog_ttl_s)

    def remote(purpose: str) -> dict[str, Any]:
        return {
            "base_url": f"{gateway}/v1",
            "api_key": None,
            "timeout_s": settings.remote_model_timeout_s,
            "headers": purpose_headers(purpose, app),
        }

    def chat(purpose: str) -> PurposeChat:
        return PurposeChat(
            gateway, purpose, purposes, app_code=app, timeout_s=settings.llm_timeout_s
        )

    return Runtime(
        settings=settings,
        registry=ModelRegistry(
            cache_dir=settings.model_cache_dir,
            remote={"embedding": remote("kb_embedding"), "rerank": remote("kb_rerank")},
        ),
        vectors=VectorFactory(qdrant),
        redis=aioredis.from_url(settings.redis_url),
        dispatcher=dispatcher or CeleryDispatcher(),
        llm=chat("kb_enrich"),
        query_llm=chat("kb_query"),
        model_purposes=purposes,
    )
