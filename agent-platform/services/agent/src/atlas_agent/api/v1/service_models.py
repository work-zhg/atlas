"""平台服务用哪个模型（模型管理方案设计第 9 章）：会话标题、记忆补记。

    GET /v1/service-models               每种用途的配置和现在能不能用          model:manage
    PUT /v1/service-models/{purpose}     配置：客户端类型 + 编码 + 模型        model:manage
    DELETE /v1/service-models/{purpose}  清除（之后这项平台功能降级）          model:manage

★ 要先在模型管理平台上建好客户端 Key、分配模型：设置时当场校验，配错了直接报。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...auth.authz import require
from ...auth.principal import Principal
from ...deps import SessionDep, SettingsDep
from ...services.service_models import ServiceModelService, ServiceModelView

router = APIRouter(prefix="/service-models", tags=["service-models"])

Manager = Annotated[Principal, Depends(require("model:manage"))]


class ServiceModelOut(BaseModel):
    purpose: str
    name: str
    client_type: str | None = None
    client_code: str | None = None
    model: str | None = None
    ready: bool
    problem: str | None = None


class ServiceModelIn(BaseModel):
    client_type: str = Field(min_length=1, max_length=64)
    client_code: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)


def _out(v: ServiceModelView) -> ServiceModelOut:
    return ServiceModelOut(**v.__dict__)


@router.get("", response_model=list[ServiceModelOut])
async def list_service_models(
    _: Manager, session: SessionDep, settings: SettingsDep
) -> list[ServiceModelOut]:
    return [_out(v) for v in await ServiceModelService(session, settings).list()]


@router.put("/{purpose}", response_model=ServiceModelOut)
async def set_service_model(
    purpose: str,
    body: ServiceModelIn,
    principal: Manager,
    session: SessionDep,
    settings: SettingsDep,
) -> ServiceModelOut:
    view = await ServiceModelService(session, settings).set(
        purpose,
        client_type=body.client_type,
        client_code=body.client_code,
        model=body.model,
        actor=principal.uid,
    )
    await session.commit()
    return _out(view)


@router.delete("/{purpose}")
async def clear_service_model(
    purpose: str, _: Manager, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    await ServiceModelService(session, settings).clear(purpose)
    await session.commit()
    return {"cleared": True}
