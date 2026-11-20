"""给应用（Atlas、知识库）的内部接口。网关（nginx）一律 404，内网免令牌。

    POST   /internal/client-keys                   创建客户端 Key（按类型 + 编码幂等）
    GET    /internal/client-keys?client_type&client_code
                                                    状态、Key 原文（只有 active 时）、可用模型
    PATCH  /internal/client-keys                   改名（对象改名时同步）
    DELETE /internal/client-keys?client_type&client_code
                                                    删除（对象删除时）
    GET    /internal/models                         已登记、启用的全部模型和能力
                                                    （还没分配时编辑器先列它们）

★ 请求头 X-Tenant-Id 必填：应用替自己的租户调。只有客户端类型的所属应用能调
  （settings.client_types，按请求头 X-Caller-App）。
★ 新智能体的 Key 建时默认分配全部公有对话模型（settings.client_key_default_public），马上能用。
★ 可用模型 = 有效模型里启用的。客户端 Key 不看用户中心的授权。
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
    models = await keys.registry.by_names()
    usable = [models[m] for m in key.effective_models or [] if m in models and models[m].enabled]
    if not usable:
        return InternalClientKeyOut(**base, status="no_models")
    return InternalClientKeyOut(
        **base,
        status="active",
        key=keys.reveal(key),
        models=[internal_model_out(m) for m in usable],
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
        default_public=body.client_type in settings.client_key_default_public,
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
    rows = [m for m in await registry.rows() if m.enabled]
    routes = await registry.routes([m.uuid for m in rows])
    accounts = await registry.accounts()
    out = []
    for m in rows:
        first = (routes.get(m.uuid) or [None])[0]
        provider = (
            accounts[first.account_uuid].provider
            if first and first.account_uuid in accounts
            else ""
        )
        out.append(internal_model_out(m, provider))
    return InternalCatalogOut(models=out)
