from fastapi import APIRouter

from . import agents, catalog, memories, meta, previews, runs, threads

router = APIRouter(prefix="/v1")
router.include_router(meta.router)
router.include_router(agents.router)
router.include_router(threads.router)
router.include_router(runs.router)
router.include_router(memories.router)
router.include_router(catalog.router)
router.include_router(previews.router)

__all__ = ["router"]
