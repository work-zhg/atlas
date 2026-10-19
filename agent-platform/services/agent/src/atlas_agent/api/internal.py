"""服务间接口 /internal/v1（不对浏览器开放：网关一律 404）。

内网免令牌（内网身份传递 §3）：调用方带着身份请求头来，这里不再校验服务间令牌。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..auth.dependency import uc_client
from ..auth.service import AuthService
from ..deps import SessionDep, SettingsDep
from ..outbound import CallIdentity, current_identity, use_identity

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


class PrincipalsBody(BaseModel):
    user_id: str


@router.post("/auth/principals")
async def principals(
    body: PrincipalsBody, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """用户 → 主体标签（知识库文档级权限 §7.4）：u:用户、d:部门（含上级）、r:角色。

    知识库按 auth-uid 展开（Agent 运行时检索 = 按数字员工判，内网身份传递 §8）。
    """
    try:
        user_id = UUID(body.user_id)
    except ValueError:
        return {"principals": [f"u:{body.user_id}"]}
    if current_identity() is None:
        # 没走主体解析：按请求头透传给用户中心（租户缺省由用户中心按 uid 推）
        uid = _uuid(request.headers.get("x-uid")) or user_id
        use_identity(
            CallIdentity(
                uid=uid,
                auth_uid=_uuid(request.headers.get("x-auth-uid")) or uid,
                tenant_id=request.headers.get("x-tenant-id") or None,
            )
        )
    svc = AuthService(session, settings, uc_client(request))
    return {"principals": await svc.principal_tags(user_id)}


def _uuid(raw: str | None) -> UUID | None:
    try:
        return UUID(raw) if raw else None
    except ValueError:
        return None
