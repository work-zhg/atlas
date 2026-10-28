"""平台服务用哪个模型（模型管理方案设计第 9 章）：会话标题、记忆补记、自动模式判断器
这类不属于某个智能体的调用。

    配置   service_model 表：每种用途一行 = 模型管理平台上的哪把客户端 Key（类型 + 编码）
           + 其中哪个模型。
           ★ 事先在平台上建好 Key、分配模型（管理员在页面上建，或平台的命令行 client-key ensure），
             再在这里配（管理接口 /v1/service-models 或命令行 service-model set）
    取用   resolve：读配置（进程内短时缓存）→ 问平台要这把 Key（平台不可用沿用上次的）→
           模型在 Key 的可用模型里 → LiteLLM + Key 的端点
           没接平台：照旧用部署配置里的模型直连 litellm

★ 平台功能出问题不该影响对话：取不到就返回 None，调用方降级（标题用问题截断、补记下次再做），
  原因记日志。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ServiceModel
from ..db.session import get_sessionmaker
from ..errors import CapabilityUnavailable, InvalidReference
from ..providers.llm.model_platform import (
    ClientKey,
    ModelEndpoint,
    ModelPlatformUnavailable,
    client_key,
    direct_endpoint,
    key_endpoint,
    uses_model_platform,
)

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "PURPOSES",
    "ResolvedModel",
    "ServiceModelService",
    "reset_service_model_cache",
    "resolve_service_model",
    "service_model_configured",
]

#: 用途 → 名称
PURPOSES: dict[str, str] = {
    "title": "会话标题",
    "memory_backfill": "记忆补记",
    "approval_judge": "自动模式判断器",
}

#: 配置的进程内缓存时间（改了配置最多滞后这么久；本进程里改的立即生效）
_TTL_S = 60.0
_cache: dict[str, tuple[float, ServiceModel | None]] = {}


def reset_service_model_cache() -> None:
    _cache.clear()


@dataclass(frozen=True)
class ResolvedModel:
    """平台功能这一次用哪个模型、怎么连。经平台时一律 Anthropic Messages（LiteLLM 转换）。"""

    model: str
    endpoint: ModelEndpoint
    provider: str = "anthropic"


async def _binding(purpose: str) -> ServiceModel | None:
    row = _cache.get(purpose)
    if row is not None and time.monotonic() - row[0] < _TTL_S:
        return row[1]
    async with get_sessionmaker()() as session:
        found = await session.get(ServiceModel, purpose)
    _cache[purpose] = (time.monotonic(), found)
    return found


async def resolve_service_model(
    settings: Settings, purpose: str, *, legacy_model: str
) -> ResolvedModel | None:
    """→ None：没配置、Key 不可用、模型不在 Key 里，或平台不可用（调用方降级）。"""
    if not uses_model_platform(settings):
        return ResolvedModel(legacy_model, direct_endpoint(settings))
    binding = await _binding(purpose)
    if binding is None:
        logger.info("平台服务「%s」还没有配置模型（service_model）", PURPOSES.get(purpose, purpose))
        return None
    try:
        key = await client_key(settings, binding.client_type, binding.client_code)
    except ModelPlatformUnavailable as exc:
        logger.warning("平台服务「%s」取不到 Key：%s", PURPOSES.get(purpose, purpose), exc)
        return None
    problem = _problem(key, binding)
    if problem is not None:
        logger.warning("平台服务「%s」用不了模型：%s", PURPOSES.get(purpose, purpose), problem)
        return None
    assert key is not None and key.key is not None
    return ResolvedModel(binding.model, key_endpoint(settings, key.key))


async def service_model_configured(settings: Settings, purpose: str) -> bool:
    """配置了没有（不管现在能不能用）。没接平台时用部署配置里的模型，算配置了。

    resolve 返回 None 时用它区分「没配置」和「配了但现在用不了」—— 判断器对两者的处理不同。
    """
    return not uses_model_platform(settings) or await _binding(purpose) is not None


def _problem(key: ClientKey | None, binding: ServiceModel) -> str | None:
    if key is None:
        return f"模型管理平台上没有客户端 Key {binding.client_type}:{binding.client_code}"
    if (why := key.problem()) is not None:
        return why
    if not key.allows(binding.model):
        return (
            f"客户端 Key {binding.client_type}:{binding.client_code} 没有分配模型 {binding.model}"
        )
    return None


@dataclass(frozen=True)
class ServiceModelView:
    purpose: str
    name: str
    client_type: str | None
    client_code: str | None
    model: str | None
    #: 现在能不能用；不能用的原因
    ready: bool
    problem: str | None


class ServiceModelService:
    """管理接口与命令行：列出、设置、清除。"""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._s = session
        self._settings = settings

    async def list(self) -> list[ServiceModelView]:
        rows = {r.purpose: r for r in await self._s.scalars(select(ServiceModel))}
        out = []
        for purpose, name in PURPOSES.items():
            row = rows.get(purpose)
            out.append(await self._view(purpose, name, row))
        return out

    async def _view(self, purpose: str, name: str, row: ServiceModel | None) -> ServiceModelView:
        if row is None:
            problem = "还没有配置" if uses_model_platform(self._settings) else None
            return ServiceModelView(purpose, name, None, None, None, problem is None, problem)
        problem: str | None
        if not uses_model_platform(self._settings):
            problem = "没有接入模型管理平台：这项配置不生效，照旧用部署配置里的模型"
        else:
            try:
                key = await client_key(self._settings, row.client_type, row.client_code)
                problem = _problem(key, row)
            except ModelPlatformUnavailable as exc:
                problem = f"模型管理平台暂时不可用：{exc}"
        return ServiceModelView(
            purpose, name, row.client_type, row.client_code, row.model, problem is None, problem
        )

    async def set(
        self,
        purpose: str,
        *,
        client_type: str,
        client_code: str,
        model: str,
        actor: UUID | None,
    ) -> ServiceModelView:
        """设置。★ 要求平台上已经有这把 Key、并分配了这个模型：配错了当场报，不等到用时才发现。"""
        if purpose not in PURPOSES:
            raise InvalidReference(f"不认识的用途：{purpose}", purpose=purpose)
        if not uses_model_platform(self._settings):
            raise CapabilityUnavailable(
                "没有接入模型管理平台（MODEL_PLATFORM_URL），不需要这项配置"
            )
        key = await client_key(self._settings, client_type, client_code)
        row = ServiceModel(
            purpose=purpose,
            client_type=client_type,
            client_code=client_code,
            model=model,
            updated_by=actor,
        )
        problem = _problem(key, row)
        if problem is not None:
            raise InvalidReference(f"现在还用不了：{problem}。请先在模型管理平台上配好")
        await self._s.merge(row)
        await self._s.flush()
        _cache.pop(purpose, None)
        return await self._view(purpose, PURPOSES[purpose], row)

    async def clear(self, purpose: str) -> None:
        row = await self._s.get(ServiceModel, purpose)
        if row is not None:
            await self._s.delete(row)
            await self._s.flush()
        _cache.pop(purpose, None)
