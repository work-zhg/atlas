"""当前主体（权限设计 §12）。登录 / 登出在用户中心，网关把身份写进请求头（内网身份传递 §4）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ...auth.authz import PrincipalDep
from ...db.models import AppUser
from ...deps import SessionDep, SettingsDep

router = APIRouter(tags=["auth"])


@router.get("/me")
async def me(principal: PrincipalDep, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    p = principal
    out: dict[str, Any] = {
        "auth_mode": settings.auth_mode,
        "id": str(p.uid),
        "name": p.name,
        "operations": sorted(p.operations),
        "menus": list(p.menus),
        # 数据权限领域：对各数据编码下全部数据的级别（前端据此显示「全部」视图）
        "data_levels": dict(p.data_levels),
    }
    user = await session.get(AppUser, p.uid)
    if user is not None:
        out.update(name=user.name, account=user.account, email=user.email, dept=user.dept_name)
    return out
