"""库里的行 → 接口里的形状。"""

from __future__ import annotations

from ..catalog import PURPOSES
from ..db.models import GwModel, GwModelCall, GwModelKey, GwModelPrice, GwModelProbe, GwPurpose
from ..keys import mask
from .schemas import (
    AccessCheckOut,
    CallOut,
    CatalogModelOut,
    KeyOut,
    ModelOut,
    PriceOut,
    ProbeOut,
    PurposeOut,
)


def price_out(row: GwModelPrice | None) -> PriceOut | None:
    if row is None:
        return None
    return PriceOut(
        id=row.uuid,
        currency=row.currency,
        input=row.input,
        output=row.output,
        cache_read=row.cache_read,
        cache_write=row.cache_write,
        per_request=row.per_request,
        tiers=list(row.tiers or []),
        effective_from=row.effective_from,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def model_out(row: GwModel, price: GwModelPrice | None = None) -> ModelOut:
    return ModelOut(
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        provider=row.provider,
        tags=list(row.tags or []),
        kind=row.kind,
        upstream_kind=row.upstream_kind,
        upstream_model=row.upstream_model,
        protocols=list(row.protocols or []),
        input_modalities=list(row.input_modalities or []),
        capabilities=dict(row.capabilities or {}),
        context_window=row.context_window,
        max_output_tokens=row.max_output_tokens,
        dimensions=row.dimensions,
        max_batch=row.max_batch,
        data_policy=dict(row.data_policy or {}),
        capacity=dict(row.capacity or {}),
        status=row.status,
        disabled=row.disabled,
        disabled_reason=row.disabled_reason,
        deprecated_at=row.deprecated_at,
        retire_at=row.retire_at,
        retired_at=row.retired_at,
        replacement=row.replacement,
        access_check=AccessCheckOut(
            ok=row.access_check_ok, detail=row.access_check_detail, checked_at=row.access_checked_at
        ),
        price=price_out(price),
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def catalog_model_out(row: GwModel, price: GwModelPrice | None) -> CatalogModelOut:
    return CatalogModelOut(
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        provider=row.provider,
        tags=list(row.tags or []),
        kind=row.kind,
        protocols=list(row.protocols or []),
        input_modalities=list(row.input_modalities or []),
        capabilities=dict(row.capabilities or {}),
        context_window=row.context_window,
        max_output_tokens=row.max_output_tokens,
        dimensions=row.dimensions,
        max_batch=row.max_batch,
        data_policy=dict(row.data_policy or {}),
        status=row.status,
        disabled=row.disabled,
        deprecated_at=row.deprecated_at,
        retire_at=row.retire_at,
        replacement=row.replacement,
        price=price_out(price),
    )


def purpose_out(row: GwPurpose) -> PurposeOut:
    spec = PURPOSES[row.code]
    return PurposeOut(
        code=spec.code,
        name=spec.name,
        kind=spec.kind,
        description=spec.description,
        callable=spec.callable,
        default_model=row.default_model,
        allowed_models=list(row.allowed_models or []),
    )


def probe_out(row: GwModelProbe) -> ProbeOut:
    return ProbeOut(
        id=row.uuid,
        created_at=row.created_at,
        created_by=row.created_by,
        results=list(row.results or []),
        suggested=dict(row.suggested or {}),
        differences=dict(row.differences or {}),
        applied_by=row.applied_by,
        applied_at=row.applied_at,
    )


def key_out(row: GwModelKey) -> KeyOut:
    return KeyOut(
        id=row.uuid,
        subject_uid=row.subject_uid,
        subject_type=row.subject_type,
        subject_name=row.subject_name,
        masked=mask(row.api_key),
        status=row.status,
        issued_by=row.issued_by,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
        disabled_at=row.disabled_at,
    )


def call_out(r: GwModelCall) -> CallOut:
    return CallOut(
        id=r.id,
        at=r.created_at,
        model=r.model,
        upstream_model=r.upstream_model,
        endpoint=r.endpoint,
        stream=r.stream,
        subject_uid=r.subject_uid,
        dept_id=r.dept_id,
        purpose=r.purpose,
        caller_app=r.caller_app,
        user_uid=r.user_uid,
        agent_id=r.agent_id,
        run_id=r.run_id,
        outcome=r.outcome,
        status=r.status,
        forwarded=r.forwarded,
        input_tokens=r.input_tokens,
        output_tokens=r.output_tokens,
        cache_read_tokens=r.cache_read_tokens,
        cache_write_tokens=r.cache_write_tokens,
        reasoning_tokens=r.reasoning_tokens,
        usage_reported=r.usage_reported,
        cost=r.cost,
        currency=r.currency,
        ttft_ms=r.ttft_ms,
        duration_ms=r.duration_ms,
    )
