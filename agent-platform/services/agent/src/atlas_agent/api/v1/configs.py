"""运行期配置的管理接口（支线 C；c4-model/infra-platform/code-appconfig.html · ConfigRoutes）。

    GET   /v1/configs                       Atlas 声明的 key：类型、默认值、是否已覆盖、生效值、版本
    GET   /v1/configs/{key}
    PUT   /v1/configs/{key}                 改值（value=null 恢复默认值），带期望版本；不对 409
    GET   /v1/configs/{key}/revisions       历史，最新的在前
    POST  /v1/configs/{key}/rollback        把某一版的值写成新版本，带期望版本

★ 看和改都要 config:manage：配额、镜像白名单这类开关只给平台管理员。修订历史里记操作人的用户 id。
★ 路由由 atlas_appconfig 给出，这里只接上 Atlas 的鉴权：依赖先按操作码判、把主体放进
  request.state，ConfigGuard 从那里取操作人。
"""

from __future__ import annotations

from typing import Annotated

from atlas_appconfig import AppConfig, ConfigRoutes
from fastapi import APIRouter, Depends, Request

from ...auth.authz import require
from ...auth.dependency import current_principal
from ...auth.principal import Principal

__all__ = ["config_router"]


async def _config_admin(
    request: Request, principal: Annotated[Principal, Depends(require("config:manage"))]
) -> None:
    request.state.config_admin = principal


class _AdminGuard:
    """鉴权在路由依赖里做完了，这里只给出操作人。"""

    async def view(self, request: Request) -> str:
        return str(request.state.config_admin.uid)

    async def edit(self, request: Request) -> str:
        return str(request.state.config_admin.uid)


def config_router(config: AppConfig) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(current_principal)])
    router.include_router(
        ConfigRoutes(config, _AdminGuard()).router, dependencies=[Depends(_config_admin)]
    )
    return router
