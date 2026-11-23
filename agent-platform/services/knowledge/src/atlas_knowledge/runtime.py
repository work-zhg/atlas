"""进程级依赖：模型、向量库、Redis、对象存储、任务投递。api 与 worker 各建一份。

测试用 Runtime(...) 直接注入假实现（内存 Qdrant、假向量模型、内联投递）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from atlas_opsapi import TenantGate

from .access.perms import DataPermissions
from .access.principals import PrincipalResolver, PrincipalTags
from .access.usercenter import UserCenterGateway
from .document.storage import FileStorage, make_storage
from .index.vdb import VectorFactory
from .models.embedding import ModelRegistry
from .models.llm import ChatModel
from .models.model_platform import PlatformChat, PlatformKeyAuth, ServiceKeys, parse_client
from .settings import KnowledgeSettings

if TYPE_CHECKING:
    from .dataset.events import DatasetEvents
    from .ops.capacity import KnowledgeCapacity

__all__ = ["Dispatcher", "Runtime", "build_runtime"]

logger = logging.getLogger(__name__)


class Dispatcher(Protocol):
    """异步任务投递。生产是 Celery；测试记下来或内联执行。

    ★ 入库一个文档一条消息：分散到各个 worker，Pod 被杀时只有正在跑的那一个停在中间状态
      （由巡检续上），不会有整批还没轮到的文档永远停在 waiting。
    """

    def indexing(self, document_uuid: str) -> None: ...

    def rebuild_index(self, version_uuid: str) -> None: ...


class CeleryDispatcher:
    def indexing(self, document_uuid: str) -> None:
        from .tasks.indexing import indexing_task

        indexing_task.delay(document_uuid)

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
    #: 调用户中心的唯一客户端（授权快照、数据权限、用户与部门）。None = 按 settings 建
    user_center_gateway: UserCenterGateway | None = None
    #: 用户 → 主体标签（文档级权限）。None = 经用户中心展开（principals.PrincipalTags）
    principal_resolver: PrincipalResolver | None = None
    #: 库级权限。None = 用户中心数据权限（就是 user_center）；测试里换成内存实现
    data_permissions: DataPermissions | None = None
    #: 租户能不能用（运营接口停用的不能用）：入库任务、巡检开始前问它。API 进程里和运营接口
    #: 用同一个（停用后本进程立刻生效）；None = 按库建（worker 进程）
    tenant_gate: TenantGate | None = None
    #: 知识库文件的存储份额（ops/capacity.py）。None = 按库建
    capacity: KnowledgeCapacity | None = None
    #: 知识库的生命周期事件与订阅方。None = 按 wiring.py 建
    dataset_events: DatasetEvents | None = None

    @property
    def events(self) -> DatasetEvents:
        if self.dataset_events is None:
            from .wiring import dataset_events

            self.dataset_events = dataset_events()
        return self.dataset_events

    @property
    def storage_capacity(self) -> KnowledgeCapacity:
        if self.capacity is None:
            from .db.session import get_sessionmaker
            from .ops.capacity import KnowledgeCapacity

            self.capacity = KnowledgeCapacity(get_sessionmaker())
        return self.capacity

    @property
    def gate(self) -> TenantGate:
        if self.tenant_gate is None:
            from .db.session import get_sessionmaker

            self.tenant_gate = TenantGate(get_sessionmaker())
        return self.tenant_gate

    @property
    def user_center(self) -> UserCenterGateway:
        if self.user_center_gateway is None:
            self.user_center_gateway = UserCenterGateway(
                self.settings.uc_base_url, snapshot_ttl_s=self.settings.authz_ttl_s
            )
        return self.user_center_gateway

    @property
    def perms(self) -> DataPermissions:
        if self.data_permissions is None:
            self.data_permissions = self.user_center
        return self.data_permissions

    async def principals(self, user_id: str) -> list[str]:
        if self.principal_resolver is None:
            tags = PrincipalTags(self.user_center, ttl_s=self.settings.principals_ttl_s)
            self.principal_resolver = tags.resolve
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
        if self.user_center_gateway is not None:
            await self.user_center_gateway.aclose()


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
    # 没接模型平台（本机开发）：只有镜像里的本地模型；没有对话模型，增强和查询理解降级为不做。
    # ★ 远程模型一律经模型平台，本服务不持有供应商 Key
    return Runtime(
        settings=settings,
        registry=ModelRegistry(cache_dir=settings.model_cache_dir),
        vectors=VectorFactory(qdrant),
        redis=aioredis.from_url(settings.redis_url),
        dispatcher=dispatcher or CeleryDispatcher(),
    )


def _platform_runtime(
    settings: KnowledgeSettings, qdrant: Any, dispatcher: Dispatcher | None
) -> Runtime:
    """经模型平台（agent-platform/doc/design/model-gateway-design.html）：各用途带着平台上
    事先建好的服务级客户端 Key 调模型网关的 OpenAI 格式入口。不持有供应商 Key。

    哪项没配客户端 Key，哪项就不可用（LLM 增强降级为不做；远程嵌入、重排不出现在建库选项里）。
    """
    import redis.asyncio as aioredis

    if not settings.model_gateway_url:
        raise ValueError("经模型平台时要配模型网关地址（ATLAS_KB_MODEL_GATEWAY_URL）")
    gateway = settings.model_gateway_url.rstrip("/")
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
            "base_url": f"{gateway}/v1",
            "api_key": None,
            "timeout_s": settings.remote_model_timeout_s,
            "auth": PlatformKeyAuth(keys, client),
        }

    def chat(model: str | None) -> PlatformChat | None:
        client = parse_client(settings.chat_client)
        if client is None or not model:
            return None
        return PlatformChat(gateway, keys, client, model, timeout_s=settings.llm_timeout_s)

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
