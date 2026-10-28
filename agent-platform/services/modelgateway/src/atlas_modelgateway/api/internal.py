"""给应用（Atlas、知识库）的内部接口（方案第 7、9 章）。网关（nginx）一律 404，内网免令牌。

    POST   /internal/client-keys                   创建客户端 Key（按类型 + 编码幂等）：「待分配」
    GET    /internal/client-keys?client_type&client_code
                                                    状态、Key 原文（只有 active 时）、可用模型
    PATCH  /internal/client-keys                   改名（对象改名时同步）
    DELETE /internal/client-keys?client_type&client_code
                                                    删除（对象删除时）；LiteLLM 里的 Key 一并删除
    GET    /internal/models                         已登记、可调用的全部模型
                                                    （还没分配时编辑器先列它们）

★ 只有客户端类型的所属应用能调（settings.client_types，按请求头 X-Caller-App）。
★ 可用模型 = 有效模型：分配的 ∩ 已登记 ∩ LiteLLM 里存在的。客户端 Key 不看用户中心的授权。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field

from ..errors import Forbidden, NotFound
from ..keys import KeyService
from ..settings import GatewaySettings
from .deps import KeysDep, RegistryDep, SettingsDep
from .schemas import InternalCatalogOut, InternalClientKeyOut
from .views import internal_model_out

router = APIRouter(prefix="/internal", tags=["internal"])

#: 缺了按「未知应用」处理（403），不回 422
CallerApp = Annotated[str, Header(alias="X-Caller-App")]


class ClientKeyCreate(BaseModel):
    client_type: str = Field(min_length=1, max_length=64)
    client_code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=200)


def _require_app(settings: GatewaySettings, client_type: str, caller: str) -> None:
    apps = settings.client_types.get(client_type)
    if apps is None:
        raise NotFound(f"没有登记的客户端类型：{client_type}", code="CLIENT_TYPE_UNKNOWN")
    if caller not in apps:
        raise Forbidden(
            f"应用 {caller or '（未知）'} 不能操作客户端类型 {client_type}",
            code="CLIENT_TYPE_FORBIDDEN",
        )


async def _view(keys: KeyService, client_type: str, client_code: str) -> InternalClientKeyOut:
    key = await keys.client_key(client_type, client_code)
    if key is None:
        raise NotFound(
            f"客户端 {client_type}:{client_code} 还没有 Key", code="CLIENT_KEY_NOT_FOUND"
        )
    base = {"client_type": client_type, "client_code": client_code, "name": key.name}
    if key.status == "pending":
        return InternalClientKeyOut(**base, status="pending_models")
    if key.status == "blocked":
        return InternalClientKeyOut(**base, status="blocked")
    views, _ = await keys.registry.listing()
    by_name = {v.name: v for v in views if v.callable}
    effective = [m for m in key.models or [] if m in by_name]
    if not effective:
        return InternalClientKeyOut(**base, status="no_models")
    return InternalClientKeyOut(
        **base,
        status="active",
        key=key.secret,
        models=[internal_model_out(by_name[m]) for m in effective],
    )


@router.post("/client-keys", response_model=InternalClientKeyOut)
async def create_client_key(
    body: ClientKeyCreate, keys: KeysDep, settings: SettingsDep, caller: CallerApp = ""
) -> InternalClientKeyOut:
    _require_app(settings, body.client_type, caller)
    await keys.ensure_client(
        client_type=body.client_type,
        client_code=body.client_code,
        name=body.name.strip(),
        actor=f"app:{caller}",
    )
    return await _view(keys, body.client_type, body.client_code.strip())


@router.get("/client-keys", response_model=InternalClientKeyOut)
async def get_client_key(
    keys: KeysDep,
    settings: SettingsDep,
    client_type: str = Query(min_length=1),
    client_code: str = Query(min_length=1),
    caller: CallerApp = "",
) -> InternalClientKeyOut:
    _require_app(settings, client_type, caller)
    return await _view(keys, client_type, client_code)


@router.patch("/client-keys", response_model=InternalClientKeyOut)
async def rename_client_key(
    body: ClientKeyCreate, keys: KeysDep, settings: SettingsDep, caller: CallerApp = ""
) -> InternalClientKeyOut:
    _require_app(settings, body.client_type, caller)
    key = await keys.client_key(body.client_type, body.client_code)
    if key is None:
        raise NotFound("客户端 Key 不存在", code="CLIENT_KEY_NOT_FOUND")
    await keys.rename_client(key, body.name.strip() or body.client_code)
    return await _view(keys, body.client_type, body.client_code)


@router.delete("/client-keys")
async def delete_client_key(
    keys: KeysDep,
    settings: SettingsDep,
    client_type: str = Query(min_length=1),
    client_code: str = Query(min_length=1),
    caller: CallerApp = "",
) -> dict[str, bool]:
    _require_app(settings, client_type, caller)
    key = await keys.client_key(client_type, client_code)
    if key is None:
        return {"deleted": False}
    await keys.delete(key, actor=f"app:{caller}")
    return {"deleted": True}


@router.get("/models", response_model=InternalCatalogOut)
async def callable_models(registry: RegistryDep) -> InternalCatalogOut:
    views, _ = await registry.listing()
    return InternalCatalogOut(models=[internal_model_out(v) for v in views if v.callable])
