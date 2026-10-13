from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal, current_principal
from ..db.session import get_session
from ..runtime import Runtime

__all__ = ["PrincipalDep", "RuntimeDep", "SessionDep"]


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime  # type: ignore[no-any-return]


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]
