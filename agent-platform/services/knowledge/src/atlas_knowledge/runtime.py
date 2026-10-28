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
from .rag.model_platform import PlatformChat, PlatformKeyAuth, ServiceKeys, parse_client
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
    #: 查询理解单独用的 LLM（经模型管理平台时可配单独的模型）。None = 和 llm 共用
    query_llm: ChatModel | None = None
    #: 经模型管理平台时取服务级客户端 Key（建库时可选的远程嵌入、重排模型 = Key 的可用模型）。
    #: None = 没接平台
    service_keys: ServiceKeys | None = None
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
        if self.service_keys is not None:
            self.service_keys.close()


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
    if settings.model_platform_url:
        return _platform_runtime(settings, qdrant, dispatcher)
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


def _platform_runtime(
    settings: KnowledgeSettings, qdrant: Any, dispatcher: Dispatcher | None
) -> Runtime:
    """经模型管理平台（agent-platform/doc/design/model-design.html 第 9 章）：各用途带着平台上
    事先建好的服务级客户端 Key 直连 LiteLLM。不持有供应商 Key。

    哪项没配客户端 Key，哪项就不可用（LLM 增强降级为不做；远程嵌入、重排不出现在建库选项里）。
    """
    import redis.asyncio as aioredis

    if not settings.model_litellm_url:
        raise ValueError("经模型管理平台时要配 LiteLLM 地址（ATLAS_KB_MODEL_LITELLM_URL）")
    litellm = settings.model_litellm_url.rstrip("/")
    keys = ServiceKeys(
        str(settings.model_platform_url),
        app_code=settings.uc_app_code,
        ttl_s=settings.model_key_ttl_s,
    )

    def remote(spec: str | None) -> dict[str, Any]:
        client = parse_client(spec)
        if client is None:
            return {}
        return {
            "base_url": f"{litellm}/v1",
            "api_key": None,
            "timeout_s": settings.remote_model_timeout_s,
            "auth": PlatformKeyAuth(keys, client),
        }

    def chat(model: str | None) -> PlatformChat | None:
        client = parse_client(settings.chat_client)
        if client is None or not model:
            return None
        return PlatformChat(litellm, keys, client, model, timeout_s=settings.llm_timeout_s)

    llm = chat(settings.enrich_model)
    return Runtime(
        settings=settings,
        registry=ModelRegistry(
            cache_dir=settings.model_cache_dir,
            remote={
                "embedding": remote(settings.embedding_client),
                "rerank": remote(settings.rerank_client),
            },
        ),
        vectors=VectorFactory(qdrant),
        redis=aioredis.from_url(settings.redis_url),
        dispatcher=dispatcher or CeleryDispatcher(),
        llm=llm,
        query_llm=chat(settings.query_model) if settings.query_model else None,
        service_keys=keys,
    )
