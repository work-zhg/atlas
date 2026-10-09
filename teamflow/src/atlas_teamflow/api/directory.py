"""选人 / 选部门：代理用户中心开放接口（只返回 TeamFlow 可访问范围内的人）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .deps import PrincipalDep, UCDep

router = APIRouter(prefix="/api/v1/directory", tags=["directory"])


@router.get("/users")
async def users(principal: PrincipalDep, uc: UCDep, q: str | None = None) -> list[dict[str, Any]]:
    return await uc.search_users(q, size=20)


@router.get("/depts")
async def depts(principal: PrincipalDep, uc: UCDep) -> list[dict[str, Any]]:
    return await uc.depts()
