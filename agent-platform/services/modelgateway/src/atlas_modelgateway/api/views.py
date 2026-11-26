"""库里的行 → 接口的响应体。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from ..accounts import AccountStatus
from ..db.models import GwAccount, GwApiKey, GwModel, GwModelCall, GwRoute
from ..keys import mask
from ..provider import ProviderCatalog, ProviderSpec
from ..shared.kernel import ModelCaps, Price
from .schemas import (
    AccountOut,
    CallOut,
    CapsIO,
    GrantOut,
    InternalModelOut,
    IssueOut,
    KeyModelOut,
    KeyOut,
    ModelOut,
    PriceIO,
    ProviderOut,
    RouteOut,
)

__all__ = [
    "account_out",
    "call_out",
    "caps_io",
    "grant_out",
    "internal_model_out",
    "key_out",
    "model_out",
    "provider_out",
]


def provider_out(spec: ProviderSpec) -> ProviderOut:
    return ProviderOut(
        code=spec.name,
        name=spec.display_name,
        formats=[f.value for f in spec.formats],
        endpoints={f.value: spec.endpoints.get(f, "") for f in spec.formats},
        can_balance=bool(spec.balance_url),
        can_list_models=spec.list_models,
        key_optional=spec.key_optional,
        needs_endpoint=spec.needs_endpoint,
        overseas=spec.overseas,
        hint=spec.model_hint,
    )


def account_out(
    row: GwAccount, status: AccountStatus, routes: int, catalog: ProviderCatalog
) -> AccountOut:
    issue = (
        IssueOut(
            kind=status.issue.issue.value,
            since=datetime.fromtimestamp(status.issue.since),
            message=status.issue.message,
        )
        if status.issue
        else None
    )
    return AccountOut(
        id=row.uuid,
        name=row.name,
        provider=row.provider,
        provider_name=catalog.spec(row.provider).display_name,
        endpoints=dict(row.endpoints or {}),
        secret_masked=f"…{row.secret_tail}" if row.secret_tail else None,
        egress=row.egress,
        first_content_timeout_s=row.first_content_timeout_s,
        balance_alert=row.balance_alert,
        balance=row.balance,
        balance_at=row.balance_at,
        remark=row.remark,
        enabled=row.enabled,
        status=status.state,
        issue=issue,
        cooling_routes=status.cooling,
        low_balance=status.low_balance,
        routes=routes,
        checked_at=row.checked_at,
        check_error=row.check_error,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def caps_io(caps: ModelCaps) -> CapsIO:
    return CapsIO(
        vision=caps.vision,
        tools=caps.tools,
        thinking=caps.thinking.value,  # type: ignore[arg-type]
        context_window=caps.context_window,
        max_output=caps.max_output,
    )


def _price_io(price: Price) -> PriceIO:
    return PriceIO(
        input=price.input,
        cache_hit=price.cache_hit,
        output=price.output,
        cache_write=price.cache_write,
    )


def model_out(
    row: GwModel,
    routes: list[GwRoute],
    accounts: Mapping[UUID, GwAccount],
    route_status: Mapping[UUID, str],
    *,
    admin: bool,
    access: str | None = None,
    grants: list[GrantOut] | None = None,
    keys: int = 0,
) -> ModelOut:
    ordered = sorted(routes, key=lambda r: (r.priority, r.created_at))
    states = [route_status.get(r.uuid, "ok") for r in ordered]
    if not row.enabled:
        status = "disabled"
    elif not states or all(s != "ok" for s in states):
        status = "unavailable"
    elif states[0] != "ok":
        status = "degraded"
    else:
        status = "available"
    return ModelOut(
        id=row.uuid,
        name=row.name,
        display_name=row.display_name or row.name,
        kind=row.kind,
        visibility=row.visibility,
        description=row.description,
        caps=caps_io(ModelCaps.from_json(row.caps)),
        price=_price_io(Price.from_json(row.price)),
        enabled=row.enabled,
        status=status,
        routes=[
            RouteOut(
                id=r.uuid,
                account_id=r.account_uuid,
                account_name=accounts[r.account_uuid].name
                if r.account_uuid in accounts
                else "（已删除）",
                provider=accounts[r.account_uuid].provider if r.account_uuid in accounts else "",
                upstream_model=r.upstream_model,
                priority=r.priority,
                weight=r.weight,
                status=s,
            )
            for r, s in zip(ordered, states, strict=True)
        ]
        if admin
        else [],
        access=access,
        grants=grants,
        keys=keys,
    )


def grant_out(raw: dict[str, Any]) -> GrantOut:
    """用户中心的一条 ACL → 授权行。"""
    subject = raw.get("subject") or {}
    return GrantOut(
        id=str(raw.get("id") or raw.get("acl_id") or ""),
        subject_type=str(raw.get("subject_type") or subject.get("type") or "").lower(),
        subject_id=str(raw.get("subject_id") or subject.get("id") or ""),
        subject_name=str(raw.get("subject_name") or subject.get("name") or ""),
        include_sub=bool(raw.get("include_sub")),
        permission=str(raw.get("permission") or raw.get("level") or ""),
    )


def key_out(
    key: GwApiKey,
    models: Mapping[str, GwModel],
    *,
    spent: Decimal = Decimal(0),
    last_used: datetime | None = None,
) -> KeyOut:
    effective = set(key.effective_models or [])

    def state(name: str) -> str:
        row = models.get(name)
        if row is None:
            return "missing"
        if name not in effective:
            return "revoked"
        return "ok" if row.enabled else "disabled"

    return KeyOut(
        id=key.uuid,
        key_type=key.key_type,
        label=key.label,
        name=key.name,
        user_id=key.user_uuid,
        client_type=key.client_type,
        client_code=key.client_code,
        models=[
            KeyModelOut(
                name=m, display_name=models[m].display_name if m in models else m, state=state(m)
            )
            for m in key.models or []
        ],
        status=key.status,
        block_reason=key.block_reason,
        masked=mask(key.secret_tail),
        monthly_budget=key.monthly_budget,
        rpm=key.rpm,
        expires_at=key.expires_at,
        spent_this_month=spent,
        last_used_at=last_used,
        created_at=key.created_at,
        rotated_at=key.rotated_at,
    )


def internal_model_out(row: GwModel, provider: str = "") -> InternalModelOut:
    caps = ModelCaps.from_json(row.caps)
    return InternalModelOut(
        name=row.name,
        display_name=row.display_name or row.name,
        kind=row.kind,
        provider=provider,
        max_input_tokens=caps.context_window,
        max_output_tokens=caps.max_output,
        supports_vision=caps.vision,
        capabilities=caps_io(caps),
    )


def call_out(row: GwModelCall) -> CallOut:
    return CallOut(
        id=row.call_uuid,
        started_at=row.started_at,
        model=row.model,
        key_id=row.key_uuid,
        key_kind=row.key_kind,
        session=row.session_key,
        entry_format=row.entry_format,
        status=row.status,
        error_category=row.error_category,
        attempts=list(row.attempts or []),
        first_content_ms=row.first_content_ms,
        duration_ms=row.duration_ms,
        input_tokens=row.input_tokens,
        cache_write_tokens=row.cache_write_tokens,
        cache_hit_tokens=row.cache_hit_tokens,
        output_tokens=row.output_tokens,
        estimated=row.estimated,
        cost=row.cost,
        notes=dict(row.notes or {}),
    )
