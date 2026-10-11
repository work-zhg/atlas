"""服务间接口 /internal/v1（不对浏览器开放）。

atlas-config 与 atlas-knowledge 直接面对浏览器，但不自己维护登录：它们把浏览器带来的会话令牌
（或服务账号密钥）交给这里校验，拿回主体（id、种类、操作码）。凭据是共享的内部令牌
（CONFIG_INTERNAL_TOKEN），不接受用户身份。
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from ..auth.dependency import uc_client
from ..auth.service import AuthService
from ..deps import SessionDep, SettingsDep
from ..errors import Unauthorized

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


class IntrospectBody(BaseModel):
    #: 浏览器会话令牌（Cookie atlas_session）
    token: str | None = None
    #: 服务账号密钥（Bearer atlas_sk_…，知识库服务的对外 API 用）
    service_key: str | None = None
    acting_user: str | None = None


@router.post("/auth/introspect")
async def introspect(
    body: IntrospectBody,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> dict[str, Any]:
    expected = settings.config_internal_token
    if (
        expected is None
        or not x_internal_token
        or not hmac.compare_digest(x_internal_token, expected.get_secret_value())
    ):
        raise Unauthorized("内部令牌无效")
    svc = AuthService(session, settings, uc_client(request))
    if body.service_key:
        p = await svc.resolve_service_key(body.service_key, body.acting_user)
    elif body.token:
        p = await svc.resolve_session(body.token)
    else:
        raise Unauthorized("缺少凭据")
    return {
        "user_id": str(p.owner_id),
        "kind": p.kind,
        "name": p.name,
        "operations": sorted(p.operations),
    }
