"""当前用户。登录 / 登出在用户中心，网关把身份写进请求头（内网身份传递 §4）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import identity
from .deps import PrincipalDep

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _me(p: identity.Principal) -> dict[str, Any]:
    return {
        "user": {
            "id": str(p.user.uuid),
            "account": p.user.account,
            "name": p.user.name,
            "email": p.user.email,
        },
        "roles": p.snapshot.get("roles", []),
        "permissions": sorted(p.permissions),
        "menus": p.snapshot.get("menus", []),
        # 数据权限领域：对各数据编码下全部数据的级别（如 {"Team": "OWNER"}）
        "data_levels": p.snapshot.get("data_levels", {}),
    }


@router.get("/me")
async def me(principal: PrincipalDep) -> dict[str, Any]:
    return _me(principal)
