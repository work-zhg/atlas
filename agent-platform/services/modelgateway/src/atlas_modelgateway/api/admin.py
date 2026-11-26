"""管理接口（经网关登录后访问，身份和租户在请求头里；数据按租户自动过滤）。

    GET    /api/v1/me                          能不能管理、网关地址（页面据此显示）
    GET    /api/v1/providers                   供应商预置：格式、默认地址、能不能查余额和拉模型列表

    账号（model:manage）
    GET    /api/v1/accounts                    列表（含状态：正常 / 冷却中 / 异常 / 已停用、余额）
    POST   /api/v1/accounts                    添加
    POST   /api/v1/accounts/test               保存前测试连接
    GET    /api/v1/accounts/{id}
    PUT    /api/v1/accounts/{id}               修改（密钥不传 = 不改）
    POST   /api/v1/accounts/{id}/test          测试连接（通过了顺带解除异常）
    POST   /api/v1/accounts/{id}/enable|disable
    DELETE /api/v1/accounts/{id}               还有线路在用的不能删

    模型
    GET    /api/v1/models                      管理员：全部（含线路和状态）；普通用户：自己能选的
    GET    /api/v1/models/caps-suggest         按账号和上游模型带出默认能力       model:manage
    POST   /api/v1/models                      登记（至少一条线路）              model:manage
    GET    /api/v1/models/{id}
    PUT    /api/v1/models/{id}                 修改（含线路）                    model:manage
    POST   /api/v1/models/{id}/enable|disable                                    model:manage
    DELETE /api/v1/models/{id}                                                   model:manage
    GET|POST /api/v1/models/{id}/grants        私有模型的授权                    model:manage
    DELETE /api/v1/models/{id}/grants/{acl}                                      model:manage
    GET    /api/v1/models/{id}/holders         实际能用的人                      model:manage

    我的 Key（一人可以多把）
    GET    /api/v1/keys/me
    POST   /api/v1/keys/me                     新建（返回原文，只这一次）
    PUT    /api/v1/keys/me/{id}                改用途名、模型、限额
    POST   /api/v1/keys/me/{id}/reset|block|unblock
    DELETE /api/v1/keys/me/{id}

    全部 Key（model:manage）
    GET    /api/v1/keys?key_type&client_type&status&q
    GET    /api/v1/client-types
    POST   /api/v1/client-keys                 新建客户端 Key（平台服务这类事先配好的）
    PUT    /api/v1/keys/{id}                   客户端 Key：分配模型、限额；个人 Key：只能改限额
    POST   /api/v1/keys/{id}/reset             只有客户端 Key（个人 Key 重置会把原文交到管理员手上）
    POST   /api/v1/keys/{id}/block|unblock     停用要填原因，持有人能看到
    DELETE /api/v1/keys/{id}

    调用记录（管理员看本租户全部，个人只看自己 Key 的）
    GET    /api/v1/calls?scope=mine|all&model&status&key_id&since&until&before&limit
    GET    /api/v1/calls/summary?by=model|key|day&scope&since&until

★ 客户端 Key 的原文不在页面上返回：应用经内部接口获取。
★ 授权写的是用户中心的数据权限，本服务只转交：操作人交给用户中心（operator_id）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query

from ..accounts import AccountInput, AccountService
from ..auth import ManagerDep, OperatorDep
from ..calls import CallFilter
from ..db.models import GwAccount, GwApiKey, GwModel, GwRoute
from ..errors import Forbidden, Invalid, NotFound
from ..keys import KeyLimits, KeyService
from ..perms import Grant
from ..registry import ModelInput, ModelRegistry, RouteInput
from ..shared.kernel import ModelCaps, ModelKind, Price, ThinkingSupport
from .deps import (
    AccountsDep,
    CallsDep,
    CatalogDep,
    HealthDep,
    KeysDep,
    PermsDep,
    ProbeClientDep,
    RegistryDep,
    SettingsDep,
)
from .schemas import (
    AccountDraftTest,
    AccountIn,
    AccountOut,
    AccountTestIn,
    BlockIn,
    CallsPage,
    CallSummaryOut,
    CapsIO,
    CapsSuggestOut,
    ClientKeyIn,
    ClientTypeOut,
    GrantIn,
    GrantOut,
    KeyIn,
    KeyOut,
    KeySecretOut,
    KeyUpdate,
    ModelIn,
    ModelOut,
    ModelUpdate,
    ProbeOut,
    ProbeStepOut,
    ProviderOut,
)
from .views import account_out, call_out, caps_io, grant_out, key_out, model_out, provider_out

router = APIRouter(prefix="/api/v1", tags=["admin"])


@router.get("/me")
async def me(operator: OperatorDep, settings: SettingsDep) -> dict[str, Any]:
    return {
        "uid": operator.uid,
        "can_manage": operator.can_manage,
        "gateway_url": settings.gateway_public_url,
    }


@router.get("/providers", response_model=list[ProviderOut])
async def providers(_: OperatorDep, catalog: CatalogDep) -> list[ProviderOut]:
    return [provider_out(s) for s in catalog.providers()]


# ════════════════════════════════════════════════════════════════════════
# 账号
# ════════════════════════════════════════════════════════════════════════


def _account_input(body: AccountIn) -> AccountInput:
    return AccountInput(
        name=body.name,
        provider=body.provider,
        endpoints=body.endpoints,
        secret=body.secret,
        egress=body.egress,
        first_content_timeout_s=body.first_content_timeout_s,
        balance_alert=body.balance_alert,
        remark=body.remark,
        enabled=body.enabled,
    )


async def _accounts_out(
    accounts: AccountService, rows: list[GwAccount], catalog: CatalogDep
) -> list[AccountOut]:
    status = await accounts.status(rows)
    counts = await accounts.route_counts()
    return [account_out(r, status[r.uuid], counts.get(r.uuid, 0), catalog) for r in rows]


def _probe_out(result: Any) -> ProbeOut:
    return ProbeOut(
        ok=result.ok,
        steps=[ProbeStepOut(name=s.name, ok=s.ok, message=s.message) for s in result.steps],
        upstream_models=result.upstream_models,
        balance=result.balance,
    )


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(
    _: ManagerDep, accounts: AccountsDep, catalog: CatalogDep
) -> list[AccountOut]:
    return await _accounts_out(accounts, await accounts.list(), catalog)


@router.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(
    body: AccountIn, operator: ManagerDep, accounts: AccountsDep, catalog: CatalogDep
) -> AccountOut:
    row = await accounts.create(_account_input(body), actor=operator.uid)
    return (await _accounts_out(accounts, [row], catalog))[0]


@router.post("/accounts/test", response_model=ProbeOut)
async def test_draft_account(
    body: AccountDraftTest, _: ManagerDep, accounts: AccountsDep, client: ProbeClientDep
) -> ProbeOut:
    data = _account_input(body)
    accounts._validate(data, creating=True)
    draft = GwAccount(
        uuid=UUID(int=0),
        name=data.name,
        provider=data.provider,
        endpoints=data.endpoints,
        egress=data.egress,
    )
    ref = accounts.ref(draft, secret=data.secret or "")
    return _probe_out(await accounts.probe(ref, client, upstream_model=body.upstream_model))


@router.get("/accounts/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: UUID, _: ManagerDep, accounts: AccountsDep, catalog: CatalogDep
) -> AccountOut:
    return (await _accounts_out(accounts, [await accounts.get(account_id)], catalog))[0]


@router.put("/accounts/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: UUID,
    body: AccountIn,
    operator: ManagerDep,
    accounts: AccountsDep,
    catalog: CatalogDep,
) -> AccountOut:
    row = await accounts.update(
        await accounts.get(account_id), _account_input(body), actor=operator.uid
    )
    return (await _accounts_out(accounts, [row], catalog))[0]


@router.post("/accounts/{account_id}/test", response_model=ProbeOut)
async def test_account(
    account_id: UUID,
    body: AccountTestIn,
    _: ManagerDep,
    accounts: AccountsDep,
    client: ProbeClientDep,
) -> ProbeOut:
    row = await accounts.get(account_id)
    result = await accounts.probe(accounts.ref(row), client, upstream_model=body.upstream_model)
    await accounts.record_check(row, result)
    return _probe_out(result)


@router.post("/accounts/{account_id}/enable", response_model=AccountOut)
async def enable_account(
    account_id: UUID, operator: ManagerDep, accounts: AccountsDep, catalog: CatalogDep
) -> AccountOut:
    row = await accounts.get(account_id)
    await accounts.set_enabled(row, True, actor=operator.uid)
    return (await _accounts_out(accounts, [row], catalog))[0]


@router.post("/accounts/{account_id}/disable", response_model=AccountOut)
async def disable_account(
    account_id: UUID, operator: ManagerDep, accounts: AccountsDep, catalog: CatalogDep
) -> AccountOut:
    row = await accounts.get(account_id)
    await accounts.set_enabled(row, False, actor=operator.uid)
    return (await _accounts_out(accounts, [row], catalog))[0]


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: UUID, operator: ManagerDep, accounts: AccountsDep
) -> dict[str, bool]:
    await accounts.delete(await accounts.get(account_id), actor=operator.uid)
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════
# 模型
# ════════════════════════════════════════════════════════════════════════


def _caps(body: CapsIO | None, kind: str) -> ModelCaps | None:
    if body is None:
        return None
    return ModelCaps(
        kind=ModelKind(kind),
        vision=body.vision,
        tools=body.tools,
        thinking=ThinkingSupport(body.thinking),
        context_window=body.context_window,
        max_output=body.max_output,
    )


def _model_input(body: ModelUpdate, kind: str) -> ModelInput:
    return ModelInput(
        display_name=body.display_name.strip(),
        kind=ModelKind(kind),
        visibility=body.visibility,
        description=body.description,
        caps=_caps(body.caps, kind),
        price=Price(
            body.price.input,
            body.price.cache_hit,
            body.price.output,
            body.price.cache_write,
        ),
        enabled=body.enabled,
        routes=[
            RouteInput(r.account_id, r.upstream_model.strip(), r.priority, r.weight)
            for r in body.routes
        ],
    )


def _grants(items: list[GrantIn]) -> list[Grant]:
    return [Grant("READ", g.subject_type, g.subject_id, g.include_sub) for g in items]


async def _route_status(
    routes: list[GwRoute], accounts: dict[UUID, GwAccount], health: HealthDep
) -> dict[UUID, str]:
    cooling, issues = await health.lookup(
        [r.uuid for r in routes], list({r.account_uuid for r in routes})
    )
    now = datetime.now(UTC).timestamp()
    out: dict[UUID, str] = {}
    for r in routes:
        account = accounts.get(r.account_uuid)
        if account is None or not account.enabled:
            out[r.uuid] = "disabled"
        elif r.account_uuid in issues:
            out[r.uuid] = "abnormal"
        elif cooling.get(r.uuid, 0) > now:
            out[r.uuid] = "cooling"
        else:
            out[r.uuid] = "ok"
    return out


async def _models_out(
    registry: ModelRegistry,
    keys: KeyService,
    health: HealthDep,
    rows: list[GwModel],
    *,
    perms: PermsDep | None = None,
) -> list[ModelOut]:
    routes = await registry.routes([m.uuid for m in rows])
    accounts = await registry.accounts()
    flat = [r for rs in routes.values() for r in rs]
    status = await _route_status(flat, accounts, health)
    grants: dict[str, list[GrantOut]] = {}
    if perms is not None:
        private = [m for m in rows if m.visibility == "private"]
        acls = await asyncio.gather(*(perms.acl(m.name) for m in private))
        grants = {
            m.name: [grant_out(a) for a in acl if str(a.get("permission")) == "READ"]
            for m, acl in zip(private, acls, strict=True)
        }
    holders: dict[str, int] = {}
    for k in await keys.search():
        for name in k.models or []:
            holders[name] = holders.get(name, 0) + 1
    return [
        model_out(
            m,
            routes.get(m.uuid, []),
            accounts,
            status,
            admin=True,
            grants=grants.get(m.name) if m.visibility == "private" and perms is not None else None,
            keys=holders.get(m.name, 0),
        )
        for m in rows
    ]


@router.get("/models", response_model=list[ModelOut])
async def list_models(
    operator: OperatorDep, registry: RegistryDep, keys: KeysDep, perms: PermsDep, health: HealthDep
) -> list[ModelOut]:
    rows = await registry.rows()
    if operator.can_manage:
        return await _models_out(registry, keys, health, rows, perms=perms)
    # 普通用户：能选进自己 Key 的（公有 + 授权给自己或所在部门的私有），停用的不显示
    usable = await keys.subjects.usable(operator.auth_uid)
    out = []
    for m in rows:
        if not m.enabled:
            continue
        if m.visibility == "public" or usable.allows(m.name):
            view = model_out(
                m,
                [],
                {},
                {},
                admin=False,
                access="public" if m.visibility == "public" else "granted",
            )
            out.append(view)
    return out


@router.get("/models/caps-suggest", response_model=CapsSuggestOut)
async def caps_suggest(
    _: ManagerDep,
    registry: RegistryDep,
    account_id: UUID,
    upstream_model: str = Query(min_length=1),
    kind: Literal["chat", "embedding", "rerank"] = "chat",
) -> CapsSuggestOut:
    account = (await registry.accounts()).get(account_id)
    if account is None:
        raise NotFound("账号不存在")
    preset = registry.presets.caps_for(account.provider, upstream_model)
    caps = registry.suggest_caps(account.provider, upstream_model, ModelKind(kind))
    return CapsSuggestOut(caps=caps_io(caps), preset=preset is not None)


@router.post("/models", response_model=ModelOut, status_code=201)
async def create_model(
    body: ModelIn,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
    perms: PermsDep,
    health: HealthDep,
) -> ModelOut:
    row = await registry.create(
        body.name, _model_input(body, body.kind), grants=_grants(body.grants), actor=operator.uid
    )
    return (await _models_out(registry, keys, health, [row], perms=perms))[0]


@router.get("/models/{model_id}", response_model=ModelOut)
async def get_model(
    model_id: UUID,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
    perms: PermsDep,
    health: HealthDep,
) -> ModelOut:
    return (await _models_out(registry, keys, health, [await registry.get(model_id)], perms=perms))[
        0
    ]


@router.put("/models/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: UUID,
    body: ModelUpdate,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
    perms: PermsDep,
    health: HealthDep,
) -> ModelOut:
    row = await registry.get(model_id)
    if await registry.update(row, _model_input(body, row.kind), actor=operator.uid):
        # 公有 ↔ 私有：勾选了它的用户 Key 的有效模型会变
        await keys.refresh_many(await keys.containing(row.name))
    return (await _models_out(registry, keys, health, [row], perms=perms))[0]


@router.post("/models/{model_id}/enable", response_model=ModelOut)
async def enable_model(
    model_id: UUID,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
    perms: PermsDep,
    health: HealthDep,
) -> ModelOut:
    row = await registry.get(model_id)
    await registry.set_enabled(row, True, actor=operator.uid)
    return (await _models_out(registry, keys, health, [row], perms=perms))[0]


@router.post("/models/{model_id}/disable", response_model=ModelOut)
async def disable_model(
    model_id: UUID,
    operator: ManagerDep,
    registry: RegistryDep,
    keys: KeysDep,
    perms: PermsDep,
    health: HealthDep,
) -> ModelOut:
    row = await registry.get(model_id)
    await registry.set_enabled(row, False, actor=operator.uid)
    return (await _models_out(registry, keys, health, [row], perms=perms))[0]


@router.delete("/models/{model_id}")
async def delete_model(
    model_id: UUID, operator: ManagerDep, registry: RegistryDep, keys: KeysDep
) -> dict[str, Any]:
    row = await registry.get(model_id)
    name = row.name
    affected = await keys.containing(name)
    await registry.delete(row, actor=operator.uid)
    for k in affected:
        k.models = [m for m in k.models or [] if m != name]
        if k.key_type == "client" and not k.models and k.status == "active":
            k.status = "pending"
    await keys.refresh_many(affected)
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
        keys.subjects.forget()
        await keys.refresh_many(await keys.containing(row.name), fresh=True)
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
    keys.subjects.forget()
    changed = await keys.refresh_many(await keys.containing(row.name), fresh=True)
    return {"revoked": acl_id, "keys_updated": changed}


@router.get("/models/{model_id}/holders")
async def holders(
    model_id: UUID, _: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> list[dict[str, Any]]:
    row = await registry.get(model_id)
    return await perms.holders(row.name)


# ════════════════════════════════════════════════════════════════════════
# Key
# ════════════════════════════════════════════════════════════════════════


def _limits(body: KeyIn | KeyUpdate, current: GwApiKey | None = None) -> KeyLimits:
    """KeyUpdate 只改传了的字段；KeyIn 全部按传的。"""
    fields = (
        body.model_fields_set
        if isinstance(body, KeyUpdate)
        else {"monthly_budget", "rpm", "expires_at"}
    )

    def pick(name: str) -> Any:
        if name in fields:
            return getattr(body, name)
        return getattr(current, name) if current is not None else None

    return KeyLimits(
        monthly_budget=pick("monthly_budget"), rpm=pick("rpm"), expires_at=pick("expires_at")
    )


async def _keys_out(
    keys: KeyService, calls: CallsDep, rows: list[GwApiKey], *, secret: str | None = None
) -> list[KeyOut]:
    models = await keys.registry.by_names()
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ids = [k.uuid for k in rows]
    spent = await calls.spent(ids, since=month)
    used = await calls.last_used(ids, since=month.replace(year=month.year - 1))
    out = [
        key_out(k, models, spent=spent.get(k.uuid, Decimal(0)), last_used=used.get(k.uuid))
        for k in rows
    ]
    if secret is not None and out:
        return [KeySecretOut(**out[0].model_dump(), secret=secret)]
    return out


@router.get("/keys/me", response_model=list[KeyOut])
async def my_keys(operator: OperatorDep, keys: KeysDep, calls: CallsDep) -> list[KeyOut]:
    return await _keys_out(keys, calls, await keys.user_keys(UUID(operator.uid)))


@router.post("/keys/me", response_model=KeySecretOut, status_code=201)
async def create_my_key(
    body: KeyIn, operator: OperatorDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key, secret = await keys.create_user_key(
        UUID(operator.uid), label=body.label, models=body.models, limits=_limits(body)
    )
    return (await _keys_out(keys, calls, [key], secret=secret))[0]


@router.put("/keys/me/{key_id}", response_model=KeyOut)
async def update_my_key(
    key_id: UUID, body: KeyUpdate, operator: OperatorDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key = await keys.user_key(UUID(operator.uid), key_id)
    await keys.update(
        key, label=body.label, models=body.models, limits=_limits(body, key), actor=operator.uid
    )
    return (await _keys_out(keys, calls, [key]))[0]


@router.post("/keys/me/{key_id}/reset", response_model=KeySecretOut)
async def reset_my_key(
    key_id: UUID, operator: OperatorDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key = await keys.user_key(UUID(operator.uid), key_id)
    secret = await keys.reset(key, actor=operator.uid)
    return (await _keys_out(keys, calls, [key], secret=secret))[0]


@router.post("/keys/me/{key_id}/block", response_model=KeyOut)
async def block_my_key(
    key_id: UUID, operator: OperatorDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key = await keys.user_key(UUID(operator.uid), key_id)
    await keys.block(key, reason="本人停用", actor=operator.uid)
    return (await _keys_out(keys, calls, [key]))[0]


@router.post("/keys/me/{key_id}/unblock", response_model=KeyOut)
async def unblock_my_key(
    key_id: UUID, operator: OperatorDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key = await keys.user_key(UUID(operator.uid), key_id)
    if key.block_reason and key.block_reason != "本人停用":
        raise Forbidden("这把 Key 是管理员停用的，请联系模型管理员")
    await keys.unblock(key, actor=operator.uid)
    return (await _keys_out(keys, calls, [key]))[0]


@router.delete("/keys/me/{key_id}")
async def delete_my_key(key_id: UUID, operator: OperatorDep, keys: KeysDep) -> dict[str, bool]:
    await keys.delete(await keys.user_key(UUID(operator.uid), key_id), actor=operator.uid)
    return {"deleted": True}


@router.get("/keys", response_model=list[KeyOut])
async def list_keys(
    _: ManagerDep,
    keys: KeysDep,
    calls: CallsDep,
    key_type: Literal["user", "client"] | None = None,
    client_type: str | None = None,
    status: str | None = Query(default=None, pattern="^(pending|active|blocked)$"),
    q: str | None = None,
) -> list[KeyOut]:
    rows = await keys.search(key_type=key_type, client_type=client_type, status=status, q=q)
    return await _keys_out(keys, calls, rows)


@router.get("/client-types", response_model=list[ClientTypeOut])
async def client_types(_: ManagerDep, settings: SettingsDep) -> list[ClientTypeOut]:
    return [
        ClientTypeOut(
            code=c, apps=list(apps), default_public=c in settings.client_key_default_public
        )
        for c, apps in settings.client_types.items()
    ]


@router.post("/client-keys", response_model=KeyOut, status_code=201)
async def create_client_key(
    body: ClientKeyIn, operator: ManagerDep, keys: KeysDep, calls: CallsDep, settings: SettingsDep
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
    await keys.update(
        key,
        label=body.label or None,
        models=body.models or None,
        limits=_limits(body),
        actor=operator.uid,
    )
    return (await _keys_out(keys, calls, [key]))[0]


@router.put("/keys/{key_id}", response_model=KeyOut)
async def update_key(
    key_id: UUID, body: KeyUpdate, operator: ManagerDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key = await keys.get(key_id)
    if key.key_type == "user" and body.models is not None:
        raise Forbidden("个人 Key 的模型由本人选，管理员只能停用、删除或改限额")
    await keys.update(
        key, label=body.label, models=body.models, limits=_limits(body, key), actor=operator.uid
    )
    return (await _keys_out(keys, calls, [key]))[0]


@router.post("/keys/{key_id}/reset", response_model=KeyOut)
async def reset_key(key_id: UUID, operator: ManagerDep, keys: KeysDep, calls: CallsDep) -> KeyOut:
    key = await keys.get(key_id)
    if key.key_type != "client":
        raise Forbidden("个人 Key 只能本人重置（重置会把新原文交到管理员手上）")
    await keys.reset(key, actor=operator.uid)
    return (await _keys_out(keys, calls, [key]))[0]


@router.post("/keys/{key_id}/block", response_model=KeyOut)
async def block_key(
    key_id: UUID, body: BlockIn, operator: ManagerDep, keys: KeysDep, calls: CallsDep
) -> KeyOut:
    key = await keys.get(key_id)
    await keys.block(key, reason=body.reason or "管理员停用", actor=operator.uid)
    return (await _keys_out(keys, calls, [key]))[0]


@router.post("/keys/{key_id}/unblock", response_model=KeyOut)
async def unblock_key(key_id: UUID, operator: ManagerDep, keys: KeysDep, calls: CallsDep) -> KeyOut:
    key = await keys.get(key_id)
    await keys.unblock(key, actor=operator.uid)
    return (await _keys_out(keys, calls, [key]))[0]


@router.delete("/keys/{key_id}")
async def delete_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> dict[str, bool]:
    await keys.delete(await keys.get(key_id), actor=operator.uid)
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════
# 调用记录
# ════════════════════════════════════════════════════════════════════════


async def _call_filter(
    operator: OperatorDep,
    keys: KeyService,
    scope: str,
    model: str | None,
    status: str | None,
    key_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
) -> CallFilter:
    if scope == "all":
        if not operator.can_manage:
            raise Forbidden("看全部调用记录要模型管理权限（model:manage）")
        key_uuids = [key_id] if key_id else None
    else:
        mine = [k.uuid for k in await keys.user_keys(UUID(operator.uid))]
        key_uuids = [k for k in mine if key_id is None or k == key_id]
    return CallFilter(key_uuids=key_uuids, model=model, status=status, since=since, until=until)


@router.get("/calls", response_model=CallsPage)
async def list_calls(
    operator: OperatorDep,
    keys: KeysDep,
    calls: CallsDep,
    scope: Literal["mine", "all"] = "mine",
    model: str | None = None,
    status: Literal["ok", "error", "interrupted"] | None = None,
    key_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> CallsPage:
    f = await _call_filter(operator, keys, scope, model, status, key_id, since, until)
    rows = await calls.page(f, limit=limit, before=before)
    return CallsPage(
        data=[call_out(r) for r in rows],
        next_before=rows[-1].started_at if len(rows) == limit else None,
    )


@router.get("/calls/summary", response_model=list[CallSummaryOut])
async def calls_summary(
    operator: OperatorDep,
    keys: KeysDep,
    calls: CallsDep,
    by: Literal["model", "key", "day"] = "model",
    scope: Literal["mine", "all"] = "mine",
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[CallSummaryOut]:
    f = await _call_filter(operator, keys, scope, None, None, None, since, until)
    rows = await calls.summary(f, by=by)
    labels: dict[str, str] = {}
    if by == "key":
        for k in await keys.search():
            labels[str(k.uuid)] = f"{k.name} · {k.label}" if k.label else k.name
    return [CallSummaryOut(**r, label=labels.get(r["group"], r["group"])) for r in rows]
