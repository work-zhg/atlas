from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from ..mcp.service import McpRegistry
from ..settings import ConfigSettings, get_settings
from ..skill.service import SkillService
from ..storage import SkillStorage, StorageNotConfigured, make_storage

__all__ = ["RegistryDep", "SessionDep", "SkillServiceDep"]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_storage(
    request: Request, settings: Annotated[ConfigSettings, Depends(get_settings)]
) -> SkillStorage:
    """进程内单例，测试可预先放进 app.state.storage（moto）。"""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        try:
            storage = make_storage(settings)
        except StorageNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        request.app.state.storage = storage
    return storage  # type: ignore[no-any-return]


def get_skill_service(
    session: SessionDep,
    storage: Annotated[SkillStorage, Depends(get_storage)],
    settings: Annotated[ConfigSettings, Depends(get_settings)],
) -> SkillService:
    return SkillService(session=session, storage=storage, settings=settings)


def get_registry(session: SessionDep) -> McpRegistry:
    return McpRegistry(session=session)


SkillServiceDep = Annotated[SkillService, Depends(get_skill_service)]
RegistryDep = Annotated[McpRegistry, Depends(get_registry)]
