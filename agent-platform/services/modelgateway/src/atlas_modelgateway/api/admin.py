"""模型管理端接口（模型管理方案设计 §11.1）。经网关（nginx）登录后访问，身份在请求头里。

    模型       登记、修改、上线、弃用、下线、停用 / 恢复、接入检查        model:manage
    探测       跑一次能力探测、看结果、确认写进目录                      model:manage
    价目       新增一版价目（只增不改）                                  model:manage
    平台用途   每种用途的默认模型和允许范围                              model:manage
    Key        停用、重置、启用（自动生成，不需要发放）                   model:manage
    授权       给人、部门、数字员工授「使用」 / 收回       该模型的所有者（用户中心校验）
    用量       报表、调用记录                            model:manage；所有者看自己的模型；
                                                        本人看自己的（/me/usage）
    额度       供应商额度与预警                                         model:manage

★ 授权写的是用户中心的数据权限，本服务只转交：操作人交给用户中心（operator_id），
  由它校验是不是所有者。模型管理员持有「全部模型 · 所有者」，所以也可以。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import Date, case, cast, func, select

from .. import audit
from ..auth import ManagerDep, Operator, OperatorDep
from ..catalog import Capabilities
from ..db.models import GwBudget, GwModel, GwModelCall, GwModelProbe
from ..db.types import utcnow
from ..errors import Forbidden, Invalid, Unavailable
from ..perms import Grant, Permissions
from ..probe import run_probe
from .deps import KeysDep, PermsDep, RecorderDep, RegistryDep, SessionDep, SettingsDep, SubjectsDep
from .schemas import (
    AlertOut,
    BudgetIn,
    BudgetOut,
    BudgetPatch,
    CallOut,
    DeprecateIn,
    DisableIn,
    GrantIn,
    GrantPatch,
    KeyOut,
    ModelIn,
    ModelOut,
    ModelPatch,
    PriceIn,
    PriceOut,
    ProbeOut,
    PurposeIn,
    PurposeOut,
    UsageGroup,
    UsageRowOut,
)
from .views import call_out, key_out, model_out, price_out, probe_out, purpose_out

router = APIRouter(prefix="/api/v1", tags=["admin"])


async def _require_owner(perms: Permissions, operator: Operator, model: str) -> None:
    """模型管理员，或该模型的所有者。"""
    if operator.can_manage:
        return
    if await perms.level(operator.auth_uid, model) != "OWNER":
        raise Forbidden(f"只有模型 {model} 的所有者或模型管理员可以查看")


async def _detail(registry: Any, row: GwModel) -> ModelOut:
    return model_out(row, await registry.current_price(row))


@router.get("/me")
async def me(operator: OperatorDep, perms: PermsDep) -> dict[str, Any]:
    """管理端据此决定显示哪些页面：模型管理员看全部；模型所有者只看自己的模型（授权、用量）。"""
    owned = await perms.owned(operator.auth_uid)
    return {
        "uid": operator.uid,
        "can_manage": operator.can_manage,
        "owned_models": sorted(owned.names),
        "owns_all": owned.all_models,
    }


# ───────────────────────────── 模型


@router.get("/models", response_model=list[ModelOut])
async def list_models(
    operator: OperatorDep, registry: RegistryDep, perms: PermsDep, kind: str | None = None
) -> list[ModelOut]:
    """模型管理员看全部；其他人看自己是所有者的模型（「模型详情 · 授权」页）。"""
    rows = await registry.list(kind=kind)
    if not operator.can_manage:
        owned = await perms.owned(operator.auth_uid)
        rows = [r for r in rows if owned.allows(r.name)]
    return [await _detail(registry, r) for r in rows]


@router.post("/models", response_model=ModelOut, status_code=201)
async def create_model(
    body: ModelIn, operator: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> ModelOut:
    attrs = body.model_dump(exclude={"name", "kind"}, exclude_none=True)
    row = await registry.create(perms, name=body.name, kind=body.kind, actor=operator.uid, **attrs)
    return await _detail(registry, row)


@router.get("/models/{name}", response_model=ModelOut)
async def get_model(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> ModelOut:
    row = await registry.get(name)
    await _require_owner(perms, operator, name)
    return await _detail(registry, row)


@router.patch("/models/{name}", response_model=ModelOut)
async def update_model(
    name: str, body: ModelPatch, operator: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> ModelOut:
    changes = body.model_dump(exclude_unset=True)
    for key in ("display_name", "upstream_model", "protocols", "capabilities"):
        if key in changes and changes[key] is None:
            raise Invalid(f"{key} 不能为空")
    row = await registry.update(name, changes, perms=perms, actor=operator.uid)
    return await _detail(registry, row)


@router.post("/models/{name}/activate", response_model=ModelOut)
async def activate_model(name: str, operator: ManagerDep, registry: RegistryDep) -> ModelOut:
    return await _detail(registry, await registry.activate(name, actor=operator.uid))


@router.post("/models/{name}/deprecate", response_model=ModelOut)
async def deprecate_model(
    name: str, body: DeprecateIn, operator: ManagerDep, registry: RegistryDep
) -> ModelOut:
    row = await registry.deprecate(
        name, replacement=body.replacement, retire_at=body.retire_at, actor=operator.uid
    )
    return await _detail(registry, row)


@router.post("/models/{name}/retire", response_model=ModelOut)
async def retire_model(name: str, operator: ManagerDep, registry: RegistryDep) -> ModelOut:
    return await _detail(registry, await registry.retire(name, actor=operator.uid))


@router.post("/models/{name}/disable", response_model=ModelOut)
async def disable_model(
    name: str, body: DisableIn, operator: ManagerDep, registry: RegistryDep
) -> ModelOut:
    row = await registry.set_disabled(name, True, reason=body.reason, actor=operator.uid)
    return await _detail(registry, row)


@router.post("/models/{name}/enable", response_model=ModelOut)
async def enable_model(name: str, operator: ManagerDep, registry: RegistryDep) -> ModelOut:
    row = await registry.set_disabled(name, False, reason="", actor=operator.uid)
    return await _detail(registry, row)


@router.post("/models/{name}/access-check", response_model=ModelOut)
async def access_check(name: str, operator: ManagerDep, registry: RegistryDep) -> ModelOut:
    row = await registry.get(name)
    await registry.check_access(row)
    return await _detail(registry, row)


# ───────────────────────────── 探测


@router.post("/models/{name}/probes", response_model=ProbeOut, status_code=201)
async def probe_model(
    name: str,
    operator: ManagerDep,
    registry: RegistryDep,
    session: SessionDep,
    settings: SettingsDep,
) -> ProbeOut:
    """跑一次能力探测（会花真实的 token）。顺带复查接入检查。"""
    row = await registry.get(name)
    await registry.check_access(row)
    try:
        outcome = await run_probe(settings, row)
    except RuntimeError as exc:
        raise Unavailable(str(exc), code="NO_GATEWAY_CREDENTIAL") from exc
    probe = GwModelProbe(
        model_uuid=row.uuid,
        results=[r.__dict__ for r in outcome.results],
        suggested=outcome.suggested,
        differences=outcome.differences,
        created_by=operator.uid,
    )
    session.add(probe)
    await session.flush()
    audit.record(
        session, actor=operator.uid, action="model.probe", target=name, probe=str(probe.uuid)
    )
    return probe_out(probe)


@router.get("/models/{name}/probes", response_model=list[ProbeOut])
async def list_probes(name: str, operator: ManagerDep, registry: RegistryDep) -> list[ProbeOut]:
    return [probe_out(p) for p in await registry.probes(await registry.get(name))]


@router.post("/models/{name}/probes/{probe_id}/apply", response_model=ModelOut)
async def apply_probe(
    name: str, probe_id: UUID, operator: ManagerDep, registry: RegistryDep
) -> ModelOut:
    return await _detail(registry, await registry.apply_probe(name, probe_id, actor=operator.uid))


# ───────────────────────────── 价目


@router.get("/models/{name}/prices", response_model=list[PriceOut])
async def list_prices(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> list[PriceOut]:
    row = await registry.get(name)
    await _require_owner(perms, operator, name)
    return [p for p in (price_out(r) for r in await registry.prices(row)) if p is not None]


@router.post("/models/{name}/prices", response_model=PriceOut, status_code=201)
async def add_price(
    name: str, body: PriceIn, operator: ManagerDep, registry: RegistryDep
) -> PriceOut:
    data = body.model_dump(exclude={"effective_from"})
    data["tiers"] = [
        {k: (str(v) if isinstance(v, Decimal) else v) for k, v in t.items()} for t in body.tiers
    ]
    row = await registry.add_price(
        name, effective_from=body.effective_from, actor=operator.uid, **data
    )
    out = price_out(row)
    assert out is not None
    return out


# ───────────────────────────── 平台用途


@router.get("/purposes", response_model=list[PurposeOut])
async def list_purposes(operator: ManagerDep, registry: RegistryDep) -> list[PurposeOut]:
    return [purpose_out(p) for p in (await registry.purposes()).values()]


@router.put("/purposes/{code}", response_model=PurposeOut)
async def set_purpose(
    code: str, body: PurposeIn, operator: ManagerDep, registry: RegistryDep
) -> PurposeOut:
    row = await registry.set_purpose(
        code,
        default_model=body.default_model,
        allowed_models=body.allowed_models,
        actor=operator.uid,
    )
    return purpose_out(row)


# ───────────────────────────── 授权


@router.get("/models/{name}/grants")
async def list_grants(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> list[dict[str, Any]]:
    await _chat_model(registry, name)
    await _require_owner(perms, operator, name)
    return await perms.acl(name)


async def _chat_model(registry: Any, name: str) -> GwModel:
    row = await registry.get(name)
    if row.kind != "chat":
        raise Invalid(f"{row.kind} 模型只供平台功能使用，不按人授权（在平台用途里配置）")
    return row


@router.post("/models/{name}/grants", status_code=201)
async def add_grant(
    name: str,
    body: GrantIn,
    operator: OperatorDep,
    registry: RegistryDep,
    perms: PermsDep,
    subjects: SubjectsDep,
) -> dict[str, bool]:
    await _chat_model(registry, name)
    await perms.grant(
        name,
        operator.uid,
        [Grant(body.level, s.type, str(s.id), body.include_sub) for s in body.subjects],
    )
    # 授给部门的影响多少人不知道：本进程的判权缓存全清（别的副本等缓存过期）
    if all(s.type == "user" for s in body.subjects):
        for s in body.subjects:
            subjects.forget(str(s.id))
    else:
        subjects.forget()
    return {"ok": True}


@router.patch("/models/{name}/grants/{acl_id}")
async def update_grant(
    name: str,
    acl_id: str,
    body: GrantPatch,
    operator: OperatorDep,
    perms: PermsDep,
    subjects: SubjectsDep,
) -> dict[str, bool]:
    await perms.update(acl_id, operator.uid, level=body.level, include_sub=body.include_sub)
    subjects.forget()
    return {"ok": True}


@router.delete("/models/{name}/grants/{acl_id}")
async def revoke_grant(
    name: str, acl_id: str, operator: OperatorDep, perms: PermsDep, subjects: SubjectsDep
) -> dict[str, bool]:
    await perms.revoke(acl_id, operator.uid)
    subjects.forget()
    return {"ok": True}


@router.get("/models/{name}/holders")
async def holders(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> list[dict[str, Any]]:
    await _chat_model(registry, name)
    await _require_owner(perms, operator, name)
    return await perms.holders(name)


# ───────────────────────────── Key


@router.get("/keys", response_model=list[KeyOut])
async def list_keys(operator: ManagerDep, keys: KeysDep) -> list[KeyOut]:
    return [key_out(k) for k in await keys.list()]


@router.post("/keys/{key_id}/reset", response_model=KeyOut)
async def reset_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    return key_out(await keys.reset(key_id, actor=operator.uid))


@router.post("/keys/{key_id}/disable", response_model=KeyOut)
async def disable_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    return key_out(await keys.disable(key_id, actor=operator.uid))


@router.post("/keys/{key_id}/enable", response_model=KeyOut)
async def enable_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    return key_out(await keys.enable(key_id, actor=operator.uid))


# ───────────────────────────── 用量


_GROUPS = {
    "model": GwModelCall.model,
    "subject": GwModelCall.subject_uid,
    "dept": GwModelCall.dept_id,
    "purpose": GwModelCall.purpose,
    "caller_app": GwModelCall.caller_app,
    "agent": GwModelCall.agent_id,
    "day": cast(GwModelCall.created_at, Date),
}


async def _usage(
    session: Any,
    group_by: UsageGroup,
    *,
    since: datetime | None,
    until: datetime | None,
    model: str | None = None,
    subject: UUID | None = None,
) -> list[UsageRowOut]:
    key = _GROUPS[group_by]
    ok = GwModelCall.outcome == "ok"

    def tokens(col: Any) -> Any:
        return func.coalesce(func.sum(col), 0)

    query = (
        select(
            key.label("key"),
            GwModelCall.currency,
            func.count().label("calls"),
            func.sum(case((ok, 0), else_=1)).label("failed"),
            tokens(GwModelCall.input_tokens),
            tokens(GwModelCall.output_tokens),
            tokens(GwModelCall.cache_read_tokens),
            tokens(GwModelCall.cache_write_tokens),
            tokens(GwModelCall.cost),
            func.sum(case((GwModelCall.usage_reported & GwModelCall.cost.is_(None), 1), else_=0)),
            func.sum(case((ok & GwModelCall.usage_reported.is_(False), 1), else_=0)),
        )
        .where(GwModelCall.endpoint.in_(("messages", "chat", "embeddings", "rerank")))
        .group_by(key, GwModelCall.currency)
        .order_by(key)
    )
    if since is not None:
        query = query.where(GwModelCall.created_at >= since)
    if until is not None:
        query = query.where(GwModelCall.created_at < until)
    if model is not None:
        query = query.where(GwModelCall.model == model)
    if subject is not None:
        query = query.where(GwModelCall.subject_uid == subject)
    out = []
    for row in (await session.execute(query)).all():
        value = row[0]
        out.append(
            UsageRowOut(
                key=None if value is None else str(value),
                currency=row[1],
                calls=int(row[2]),
                failed=int(row[3] or 0),
                input_tokens=int(row[4]),
                output_tokens=int(row[5]),
                cache_read_tokens=int(row[6]),
                cache_write_tokens=int(row[7]),
                cost=Decimal(str(row[8] or 0)),
                unpriced=int(row[9] or 0),
                unreported=int(row[10] or 0),
            )
        )
    return out


@router.get("/usage", response_model=list[UsageRowOut])
async def usage(
    operator: OperatorDep,
    session: SessionDep,
    perms: PermsDep,
    group_by: UsageGroup = "model",
    since: datetime | None = None,
    until: datetime | None = None,
    model: str | None = None,
    subject: UUID | None = None,
) -> list[UsageRowOut]:
    """用量报表。模型管理员看全部；模型所有者带上 model 看自己的模型。"""
    if not operator.can_manage:
        if model is None:
            raise Forbidden("只能按自己是所有者的模型查看用量（带上 model）")
        await _require_owner(perms, operator, model)
    return await _usage(session, group_by, since=since, until=until, model=model, subject=subject)


@router.get("/me/usage", response_model=list[UsageRowOut])
async def my_usage(
    operator: OperatorDep,
    session: SessionDep,
    group_by: UsageGroup = "model",
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[UsageRowOut]:
    """本人的用量（以人为权限的调用；以 Agent 为权限的算在数字员工头上）。"""
    try:
        me_ = UUID(operator.uid)
    except ValueError as exc:
        raise Invalid("当前用户不是用户中心的用户 id") from exc
    return await _usage(session, group_by, since=since, until=until, subject=me_)


@router.get("/calls", response_model=list[CallOut])
async def calls(
    operator: OperatorDep,
    session: SessionDep,
    perms: PermsDep,
    model: str | None = None,
    subject: UUID | None = None,
    purpose: str | None = None,
    outcome: str | None = None,
    before: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[CallOut]:
    """调用记录（访问日志）。模型管理员看全部；模型所有者带上 model 看自己的模型。"""
    if not operator.can_manage:
        if model is None:
            raise Forbidden("只能按自己是所有者的模型查看调用记录（带上 model）")
        await _require_owner(perms, operator, model)
    query = select(GwModelCall).order_by(GwModelCall.created_at.desc()).limit(limit)
    if model is not None:
        query = query.where(GwModelCall.model == model)
    if subject is not None:
        query = query.where(GwModelCall.subject_uid == subject)
    if purpose is not None:
        query = query.where(GwModelCall.purpose == purpose)
    if outcome is not None:
        query = query.where(GwModelCall.outcome == outcome)
    if before is not None:
        query = query.where(GwModelCall.created_at < before)
    return [call_out(r) for r in await session.scalars(query)]


# ───────────────────────────── 额度、告警


async def _budget_out(registry: Any, row: Any) -> BudgetOut:
    spent, unpriced = await registry.spent(row)
    remaining = row.amount - spent
    ratio = float(remaining / row.amount) if row.amount else 0.0
    return BudgetOut(
        id=row.uuid,
        name=row.name,
        provider=row.provider,
        amount=row.amount,
        currency=row.currency,
        starts_at=row.starts_at,
        warn_ratio=row.warn_ratio,
        note=row.note,
        active=row.active,
        spent=spent,
        remaining=remaining,
        remaining_ratio=ratio,
        unpriced_calls=unpriced,
        warning=row.active and ratio <= row.warn_ratio,
    )


@router.get("/budgets", response_model=list[BudgetOut])
async def list_budgets(operator: ManagerDep, registry: RegistryDep) -> list[BudgetOut]:
    return [await _budget_out(registry, b) for b in await registry.budgets()]


@router.post("/budgets", response_model=BudgetOut, status_code=201)
async def create_budget(
    body: BudgetIn, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> BudgetOut:
    row = GwBudget(
        name=body.name,
        provider=body.provider,
        amount=body.amount,
        currency=body.currency,
        starts_at=body.starts_at or utcnow(),
        warn_ratio=body.warn_ratio,
        note=body.note,
        created_by=operator.uid,
        updated_by=operator.uid,
    )
    session.add(row)
    await session.flush()
    audit.record(session, actor=operator.uid, action="budget.create", target=str(row.uuid))
    return await _budget_out(registry, row)


@router.patch("/budgets/{budget_id}", response_model=BudgetOut)
async def update_budget(
    budget_id: UUID,
    body: BudgetPatch,
    operator: ManagerDep,
    registry: RegistryDep,
    session: SessionDep,
) -> BudgetOut:
    row = await registry.budget(budget_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None:
            raise Invalid(f"{key} 不能为空")
        setattr(row, key, value)
    row.updated_by = operator.uid
    audit.record(
        session,
        actor=operator.uid,
        action="budget.update",
        target=str(row.uuid),
        changes={k: str(v) for k, v in changes.items()},
    )
    return await _budget_out(registry, row)


@router.get("/alerts", response_model=list[AlertOut])
async def alerts(
    operator: ManagerDep, registry: RegistryDep, session: SessionDep, recorder: RecorderDep
) -> list[AlertOut]:
    """需要模型管理员处理的事（§10.3、§12）。都应当为零。"""
    out: list[AlertOut] = []
    for budget in await registry.budgets():
        b = await _budget_out(registry, budget)
        if b.warning:
            out.append(
                AlertOut(
                    kind="budget_low",
                    message=f"额度「{b.name}」估算剩余 {b.remaining} {b.currency}"
                    f"（{b.remaining_ratio:.0%}）",
                    target=str(b.id),
                )
            )
    for row in await registry.list():
        live = row.status in ("available", "deprecated") and not row.disabled
        if row.access_check_ok is False:
            out.append(
                AlertOut(
                    kind="access_check_failed",
                    message=f"模型 {row.name} 的上游不带网关凭据也能调通：可以绕过模型网关",
                    target=row.name,
                )
            )
        if live and await registry.current_price(row) is None:
            out.append(
                AlertOut(
                    kind="unpriced", message=f"模型 {row.name} 没有价格：成本未知", target=row.name
                )
            )
        if (
            live
            and row.kind == "chat"
            and not Capabilities.model_validate(row.capabilities or {}).stream_usage
        ):
            out.append(
                AlertOut(
                    kind="stream_usage_missing",
                    message=f"模型 {row.name} 流式时不回报用量：用量和成本只能估算",
                    target=row.name,
                )
            )
    since = utcnow() - timedelta(hours=1)
    rejected = await session.scalar(
        select(func.count())
        .select_from(GwModelCall)
        .where(GwModelCall.created_at >= since, GwModelCall.outcome == "upstream_rejected")
    )
    if rejected:
        out.append(
            AlertOut(
                kind="upstream_rejected",
                message=f"最近一小时有 {rejected} 次请求被上游拒绝了网关凭据（网关配置事故）",
            )
        )
    exhausted = await session.scalar(
        select(func.count())
        .select_from(GwModelCall)
        .where(GwModelCall.created_at >= since, GwModelCall.status == 402)
    )
    if exhausted:
        out.append(
            AlertOut(
                kind="provider_402",
                message=f"最近一小时有 {exhausted} 次请求被供应商以 402 拒绝：额度可能已经用完",
            )
        )
    if recorder.pending:
        out.append(
            AlertOut(
                kind="usage_pending",
                message=f"本副本有 {len(recorder.pending)} 条用量记录还没写进库",
            )
        )
    return out


__all__ = ["router"]
