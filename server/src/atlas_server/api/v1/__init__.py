from fastapi import APIRouter, Depends

from ...auth.dependency import current_principal
from . import agents, auth, catalog, memories, meta, previews, runs, service_accounts, threads

router = APIRouter(prefix="/v1")
# ★ 除登录接口与文件预览站点（靠路径里的预览令牌）外，全部要求已认证的主体
#   （权限设计 §4）：路由自己没声明身份依赖的（如模型目录）也不能匿名访问。
_authed = [Depends(current_principal)]
router.include_router(auth.router)
router.include_router(meta.router, dependencies=_authed)
router.include_router(agents.router, dependencies=_authed)
router.include_router(threads.router, dependencies=_authed)
router.include_router(runs.router, dependencies=_authed)
router.include_router(memories.router, dependencies=_authed)
router.include_router(catalog.router, dependencies=_authed)
router.include_router(previews.router)
router.include_router(service_accounts.router, dependencies=_authed)

__all__ = ["router"]
