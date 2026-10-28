from fastapi import APIRouter, Depends

from ...auth.dependency import current_principal
from . import (
    agents,
    auth,
    catalog,
    directory,
    memories,
    meta,
    previews,
    runs,
    service_models,
    threads,
)

router = APIRouter(prefix="/v1")
# ★ 除文件预览站点（靠路径里的预览令牌）外，全部要求已认证的主体（权限设计 §4）：
#   路由自己没声明身份依赖的（如模型目录）也不能匿名访问。登录在网关 + 用户中心。
_authed = [Depends(current_principal)]
router.include_router(auth.router, dependencies=_authed)
router.include_router(meta.router, dependencies=_authed)
router.include_router(agents.router, dependencies=_authed)
router.include_router(threads.router, dependencies=_authed)
router.include_router(runs.router, dependencies=_authed)
router.include_router(memories.router, dependencies=_authed)
router.include_router(catalog.router, dependencies=_authed)
router.include_router(previews.router)
router.include_router(directory.router, dependencies=_authed)
router.include_router(service_models.router, dependencies=_authed)

__all__ = ["router"]
