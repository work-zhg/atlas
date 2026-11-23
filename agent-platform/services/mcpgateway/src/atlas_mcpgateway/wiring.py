"""组合根：按一个会话拼出各领域的应用服务（接口依赖、运维命令行、维护任务共用）。

    服务目录 ← CatalogEventHandler（授权实现目录事件）
    授权     ← 用户中心防腐层、判权缓存、服务目录的仓储（校验工具名）
    API Key  ← 授权（登记、停用主体）

★ 依赖只往下：API Key → 授权 → 服务目录 → 上游接入；目录经 CatalogEvents 让授权改用户中心。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .access.authorization import Authorization
from .access.handler import CatalogEventHandler
from .access.key_access import AccessCache
from .access.usercenter import UserCenter
from .catalog.repository import ServiceRepository
from .catalog.service import ServiceCatalog
from .keys.service import ApiKeys
from .settings import GatewaySettings
from .upstream.gateway import UpstreamGateway

__all__ = ["api_keys", "authorization", "catalog"]


def catalog(
    session: AsyncSession, settings: GatewaySettings, uc: UserCenter, upstream: UpstreamGateway
) -> ServiceCatalog:
    return ServiceCatalog(session, settings, upstream, CatalogEventHandler(session, uc))


def authorization(session: AsyncSession, uc: UserCenter, cache: AccessCache) -> Authorization:
    return Authorization(session, uc, cache, ServiceRepository(session))


def api_keys(session: AsyncSession, uc: UserCenter, cache: AccessCache) -> ApiKeys:
    return ApiKeys(session, authorization(session, uc, cache))
