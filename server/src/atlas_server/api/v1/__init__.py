from fastapi import APIRouter

from . import agents, memories, meta, runs, threads

router = APIRouter(prefix="/v1")
router.include_router(meta.router)
router.include_router(agents.router)
router.include_router(threads.router)
router.include_router(runs.router)
router.include_router(memories.router)

__all__ = ["router"]
