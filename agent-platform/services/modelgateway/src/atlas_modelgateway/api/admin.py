"""管理端接口（方案第 11 章）。经网关（nginx）登录后访问，身份在请求头里。

    GET    /api/v1/me                                    能不能管理（页面据此显示）
    GET    /api/v1/providers                             供应商列表（登记表单）
    GET    /api/v1/models                                管理员：合并列表；普通用户：能选的模型
    POST   /api/v1/models                                登记                       model:manage
    POST   /api/v1/models/test-connection                测试连接                    model:manage
    POST   /api/v1/models/adopt                          纳入 LiteLLM 直配的模型      model:manage
    PUT    /api/v1/models/{id}                           编辑                       model:manage
    POST   /api/v1/models/{id}/reregister                LiteLLM 中缺失时重新登记     model:manage
    DELETE /api/v1/models/{id}                           删除                       model:manage
    GET    /api/v1/models/{id}/grants                    私有模型的授权              model:manage
    POST   /api/v1/models/{id}/grants                    授权（用户、部门）           model:manage
    DELETE /api/v1/models/{id}/grants/{acl}              收回                       model:manage
    GET    /api/v1/models/{id}/holders                   实际能用的人                model:manage

    GET    /api/v1/keys/me                               我的用户 Key
    POST   /api/v1/keys/me                               申请（返回原文，只这一次）
    PUT    /api/v1/keys/me/models                        改模型
    POST   /api/v1/keys/me/reset|block|unblock           重置（返回原文）/ 停用 / 启用
    DELETE /api/v1/keys/me                               删除

    GET    /api/v1/client-types                          客户端类型                  model:manage
    GET    /api/v1/client-keys                           客户端 Key（类型、状态、搜索）model:manage
    POST   /api/v1/client-keys                           新建（平台服务这类事先配好的）model:manage
    PUT    /api/v1/client-keys/{id}/models               分配模型（第一次分配时生成）  model:manage
    POST   /api/v1/client-keys/{id}/reset|block|unblock                             model:manage
    DELETE /api/v1/client-keys/{id}                                                 model:manage

★ 客户端 Key 的原文不在页面上返回：应用经内部接口获取。
★ 授权写的是用户中心的数据权限，本服务只转交：操作人交给用户中心（operator_id）。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query

from ..auth import ManagerDep, OperatorDep
from ..db.models import GwApiKey
from ..errors import Invalid, NotFound
from ..keys import KeyService
from ..perms import Grant
from ..registry import PROVIDERS, ModelFields, ModelRegistry, RouteFields
from .deps import KeysDep, PermsDep, RegistryDep, SettingsDep
from .schemas import (
    ClientKeyIn,
    ClientTypeOut,
    ConnectionTestIn,
    ConnectionTestOut,
    GrantIn,
    GrantOut,
    KeyOut,
    KeySecretOut,
    ModelAdopt,
    ModelIn,
    ModelListOut,
    ModelOut,
    ModelReregister,
    ModelsIn,
    ModelUpdate,
    ProviderOut,
    RouteIn,
)
from .views import grant_out, key_out, model_out

router = APIRouter(prefix="/api/v1", tags=["admin"])


def _route(body: RouteIn) -> RouteFields:
    return RouteFields(
        api_base=(body.api_base or "").strip() or None,
        api_key=(body.api_key or "").strip() or None,
        max_input_tokens=body.max_input_tokens,
        max_output_tokens=body.max_output_tokens,
        input_price=body.input_price,
        output_price=body.output_price,
    )


def _grants(items: list[GrantIn]) -> list[Grant]:
    return [Grant("READ", g.subject_type, g.subject_id, g.include_sub) for g in items]


@router.get("/me")
async def me(operator: OperatorDep, settings: SettingsDep) -> dict[str, Any]:
    return {
        "uid": operator.uid,
        "can_manage": operator.can_manage,
        "litellm_url": settings.litellm_public_url,
    }


@router.get("/providers", response_model=list[ProviderOut])
async def providers(_: OperatorDep) -> list[ProviderOut]:
    return [
        ProviderOut(
            code=code,
            name=spec["name"],
            needs_base=bool(spec.get("needs_base")),
            key_optional=bool(spec.get("key_optional")),
        )
        for code, spec in PROVIDERS.items()
    ]


# ── 模型


@router.get("/models", response_model=ModelListOut)
async def list_models(
    operator: OperatorDep, registry: RegistryDep, keys: KeysDep, perms: PermsDep
) -> ModelListOut:
    views, reachable = await registry.listing(fresh=True)
    if operator.can_manage:
        private = [v for v in views if v.row is not None and v.row.visibility == "private"]
        acls = await asyncio.gather(*(perms.acl(v.name) for v in private))
        grants = {
            v.name: [grant_out(a) for a in acl if str(a.get("permission")) == "READ"]
            for v, acl in zip(private, acls, strict=True)
        }
        data = [model_out(v, admin=True, grants=grants.get(v.name)) for v in views]
    else:
        # 普通用户：能选进自己用户 Key 的（公有 + 授权给自己的私有）；LiteLLM 中缺失的也列出来
        allowed = await keys.subjects.usable(operator.auth_uid)
        data = [
            model_out(
                v, admin=False, access="public" if v.row.visibility == "public" else "granted"
            )
            for v in views
            if v.row is not None and (v.row.visibility == "public" or allowed.allows(v.name))
        ]
    return ModelListOut(data=data, litellm_reachable=reachable)


@router.post("/models/test-connection", response_model=ConnectionTestOut)
async def test_connection(
    body: ConnectionTestIn, _: ManagerDep, registry: RegistryDep
) -> ConnectionTestOut:
    ok, message = await registry.test_connection(
        name=body.name.strip(), provider=body.provider, route=_route(body)
    )
    return ConnectionTestOut(ok=ok, message=message)


@router.post("/models", response_model=ModelOut, status_code=201)
async def register_model(body: ModelIn, operator: ManagerDep, registry: RegistryDep) -> ModelOut:
    row = await registry.register(
        name=body.name.strip(),
        fields=ModelFields(
            body.display_name.strip(), body.provider, body.visibility, body.description
        ),
        route=_route(body),
        grants=_grants(body.grants),
        actor=operator.uid,
    )
    return await _one(registry, row.uuid)


@router.post("/models/adopt", response_model=ModelOut, status_code=201)
async def adopt_model(body: ModelAdopt, operator: ManagerDep, registry: RegistryDep) -> ModelOut:
    row = await registry.adopt(
        name=body.name.strip(),
        fields=ModelFields(body.display_name.strip(), "", body.visibility, body.description),
        grants=_grants(body.grants),
        actor=operator.uid,
    )
    return await _one(registry, row.uuid)


@router.put("/models/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: UUID,
    body: ModelUpdate,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
) -> ModelOut:
    row = await registry.get(model_id)
    changed = await registry.update(
        row,
        fields=ModelFields(
            body.display_name.strip(), body.provider, body.visibility, body.description
        ),
        route=_route(body) if row.litellm_model_id is not None else None,
        actor=operator.uid,
    )
    if changed:
        # 公有 ↔ 私有：分配了它的用户 Key 的有效模型会变
        await keys.resync(await keys.containing(row.name))
    return await _one(registry, row.uuid)


@router.post("/models/{model_id}/reregister", response_model=ModelOut)
async def reregister_model(
    model_id: UUID,
    body: ModelReregister,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
) -> ModelOut:
    row = await registry.reregister(
        await registry.get(model_id), provider=body.provider, route=_route(body), actor=operator.uid
    )
    await keys.resync(await keys.containing(row.name))
    return await _one(registry, row.uuid)


@router.delete("/models/{model_id}")
async def delete_model(
    model_id: UUID, operator: ManagerDep, registry: RegistryDep, keys: KeysDep
) -> dict[str, Any]:
    row = await registry.get(model_id)
    name = row.name
    await registry.delete(row, actor=operator.uid)
    affected = await keys.drop_model(name)
    await keys.resync(affected)
    return {"deleted": name, "keys_updated": len(affected)}


@router.get("/models/{model_id}/grants", response_model=list[GrantOut])
async def list_grants(
    model_id: UUID, _: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> list[GrantOut]:
    row = await registry.get(model_id)
    return [grant_out(a) for a in await perms.acl(row.name)]


@router.post("/models/{model_id}/grants", response_model=list[GrantOut])
async def add_grants(
    model_id: UUID,
    body: list[GrantIn],
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
) -> list[GrantOut]:
    row = await registry.get(model_id)
    if row.visibility != "private":
        raise Invalid("公有模型不需要授权")
    if body:
        await perms.grant(row.name, operator.uid, _grants(body))
        await keys.resync(await keys.containing(row.name))
    return [grant_out(a) for a in await perms.acl(row.name)]


@router.delete("/models/{model_id}/grants/{acl_id}")
async def revoke_grant(
    model_id: UUID,
    acl_id: str,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
) -> dict[str, Any]:
    row = await registry.get(model_id)
    await perms.revoke(acl_id, operator.uid)
    changed = await keys.resync(await keys.containing(row.name))
    return {"revoked": acl_id, "keys_updated": changed}


@router.get("/models/{model_id}/holders")
async def holders(
    model_id: UUID, _: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> list[dict[str, Any]]:
    row = await registry.get(model_id)
    return await perms.holders(row.name)


async def _one(registry: ModelRegistry, model_id: UUID) -> ModelOut:
    views, _ = await registry.listing()
    view = next(v for v in views if v.row is not None and v.row.uuid == model_id)
    return model_out(view, admin=True)


# ── 我的用户 Key


async def _key_view(keys: KeyService, key: GwApiKey, secret: str | None = None) -> KeyOut:
    views, _ = await keys.registry.listing()
    allowed = await keys.allowed(key.key_type, key.user_uuid)
    return key_out(
        key,
        allowed=set(allowed),
        callable_names={v.name for v in views if v.callable},
        platform_names={v.name for v in views if v.row is not None},
        secret=secret,
    )


async def _my_key(keys: KeyService, operator: OperatorDep) -> GwApiKey:
    key = await keys.user_key(UUID(operator.uid))
    if key is None:
        raise NotFound("你还没有用户 Key")
    return key


@router.get("/keys/me", response_model=KeyOut | None)
async def my_key(operator: OperatorDep, keys: KeysDep) -> KeyOut | None:
    key = await keys.user_key(UUID(operator.uid))
    return await _key_view(keys, key) if key is not None else None


@router.post("/keys/me", response_model=KeySecretOut, status_code=201)
async def apply_key(body: ModelsIn, operator: OperatorDep, keys: KeysDep) -> KeyOut:
    key, secret = await keys.apply(UUID(operator.uid), models=body.models)
    return await _key_view(keys, key, secret)


@router.put("/keys/me/models", response_model=KeyOut)
async def my_key_models(body: ModelsIn, operator: OperatorDep, keys: KeysDep) -> KeyOut:
    key = await _my_key(keys, operator)
    await keys.assign(key, body.models, actor=operator.uid)
    return await _key_view(keys, key)


@router.post("/keys/me/reset", response_model=KeySecretOut)
async def my_key_reset(operator: OperatorDep, keys: KeysDep) -> KeyOut:
    key = await _my_key(keys, operator)
    secret = await keys.reset(key, actor=operator.uid)
    return await _key_view(keys, key, secret)


@router.post("/keys/me/block", response_model=KeyOut)
async def my_key_block(operator: OperatorDep, keys: KeysDep) -> KeyOut:
    key = await _my_key(keys, operator)
    await keys.block(key, actor=operator.uid)
    return await _key_view(keys, key)


@router.post("/keys/me/unblock", response_model=KeyOut)
async def my_key_unblock(operator: OperatorDep, keys: KeysDep) -> KeyOut:
    key = await _my_key(keys, operator)
    await keys.unblock(key, actor=operator.uid)
    return await _key_view(keys, key)


@router.delete("/keys/me")
async def my_key_delete(operator: OperatorDep, keys: KeysDep) -> dict[str, bool]:
    await keys.delete(await _my_key(keys, operator), actor=operator.uid)
    return {"deleted": True}


# ── 客户端 Key（管理员）


@router.get("/client-types", response_model=list[ClientTypeOut])
async def client_types(_: ManagerDep, settings: SettingsDep) -> list[ClientTypeOut]:
    return [ClientTypeOut(code=c, apps=list(apps)) for c, apps in settings.client_types.items()]


@router.get("/client-keys", response_model=list[KeyOut])
async def list_client_keys(
    _: ManagerDep,
    keys: KeysDep,
    client_type: str | None = None,
    status: str | None = Query(default=None, pattern="^(pending|active|blocked)$"),
    q: str | None = None,
) -> list[KeyOut]:
    rows = await keys.client_keys(client_type=client_type, status=status, q=q)
    views, _ = await keys.registry.listing()
    callable_names = {v.name for v in views if v.callable}
    platform_names = {v.name for v in views if v.row is not None}
    return [
        key_out(
            k,
            allowed=callable_names,
            callable_names=callable_names,
            platform_names=platform_names,
        )
        for k in rows
    ]


@router.post("/client-keys", response_model=KeyOut, status_code=201)
async def create_client_key(
    body: ClientKeyIn, operator: ManagerDep, keys: KeysDep, settings: SettingsDep
) -> KeyOut:
    if body.client_type not in settings.client_types:
        raise Invalid(f"没有登记的客户端类型：{body.client_type}")
    key, created = await keys.ensure_client(
        client_type=body.client_type,
        client_code=body.client_code,
        name=body.name.strip(),
        actor=operator.uid,
    )
    if not created:
        raise Invalid(f"客户端 {body.client_type}:{body.client_code} 已经有 Key 了")
    return await _key_view(keys, key)


async def _client_key(keys: KeyService, key_id: UUID) -> GwApiKey:
    key = await keys.get(key_id)
    if key.key_type != "client":
        raise NotFound("客户端 Key 不存在")
    return key


@router.put("/client-keys/{key_id}/models", response_model=KeyOut)
async def assign_client_key(
    key_id: UUID, body: ModelsIn, operator: ManagerDep, keys: KeysDep
) -> KeyOut:
    key = await _client_key(keys, key_id)
    await keys.assign(key, body.models, actor=operator.uid)
    return await _key_view(keys, key)


@router.post("/client-keys/{key_id}/reset", response_model=KeyOut)
async def reset_client_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    key = await _client_key(keys, key_id)
    await keys.reset(key, actor=operator.uid)
    return await _key_view(keys, key)


@router.post("/client-keys/{key_id}/block", response_model=KeyOut)
async def block_client_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    key = await _client_key(keys, key_id)
    await keys.block(key, actor=operator.uid)
    return await _key_view(keys, key)


@router.post("/client-keys/{key_id}/unblock", response_model=KeyOut)
async def unblock_client_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    key = await _client_key(keys, key_id)
    await keys.unblock(key, actor=operator.uid)
    return await _key_view(keys, key)


@router.delete("/client-keys/{key_id}")
async def delete_client_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> dict[str, bool]:
    await keys.delete(await _client_key(keys, key_id), actor=operator.uid)
    return {"deleted": True}
