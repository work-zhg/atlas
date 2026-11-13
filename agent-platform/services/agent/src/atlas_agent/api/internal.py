"""给集群内其他服务用的接口（c4-model/agent/device/code-device-registry.html · DeviceAuthRoutes）。

    POST /internal/devices/redeem         设备网关：连接器拿配对码换设备令牌
    POST /internal/devices/authenticate   设备网关：核对设备令牌（每次建立控制连接）

★ 只走集群内地址：前端网关（nginx）把 /atlas-api/internal/ 挡在外面。
★ 不需要登录：凭据就是请求体里的配对码、设备令牌。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..deps import RedisDep, SessionDep, SettingsDep
from ..device.registry import DeviceService

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


class RedeemIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    report: dict[str, Any] = Field(default_factory=dict)


class AuthenticateIn(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    report: dict[str, Any] = Field(default_factory=dict)


@router.post("/devices/redeem")
async def redeem_pairing_code(
    body: RedeemIn, session: SessionDep, redis: RedisDep, settings: SettingsDep
) -> JSONResponse:
    """码有效就建设备、发令牌：{device_uuid, owner_uuid, name, token}。令牌原文只返回这一次。"""
    service = DeviceService(session, redis=redis, pairing_ttl_s=settings.device_pairing_ttl_s)
    issued = await service.redeem(body.code, body.report)
    if issued is None:
        return JSONResponse(
            {"error": {"kind": "invalid_code", "message": "配对码无效或已过期"}}, status_code=400
        )
    return JSONResponse(issued.to_json())


@router.post("/devices/authenticate")
async def authenticate_device(body: AuthenticateIn, session: SessionDep) -> JSONResponse:
    """→ {device_uuid, owner_uuid, name}；令牌不对或设备已吊销回 401。"""
    identity = await DeviceService(session).authenticate(body.token, body.report)
    if identity is None:
        return JSONResponse(
            {"error": {"kind": "unauthorized", "message": "设备令牌无效或设备已吊销"}},
            status_code=401,
        )
    return JSONResponse(identity.to_json())
