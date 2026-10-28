"""库里的行、LiteLLM 的部署 → 接口里的形状。"""

from __future__ import annotations

from typing import Any

from ..db.models import GwApiKey, GwModel
from ..keys import mask
from ..registry import ModelView
from .schemas import GrantOut, InternalModelOut, KeyModelOut, KeyOut, KeySecretOut, ModelOut


def _price(per_token: float | None) -> float | None:
    """美元 / token → 美元 / 百万 token（保留到分以下两位，避免 0.28 变成 0.27999999）。"""
    return None if per_token is None else round(per_token * 1e6, 6)


def model_out(
    view: ModelView,
    *,
    admin: bool,
    grants: list[GrantOut] | None = None,
    access: str | None = None,
) -> ModelOut:
    row, dep = view.row, view.primary
    status = "unknown" if view.unknown else "missing" if view.missing else "ok"
    return ModelOut(
        uuid=row.uuid if row is not None else None,
        name=view.name,
        display_name=(row.display_name if row is not None else view.name) or view.name,
        provider=(row.provider if row is not None else dep.provider if dep else "") or "",
        visibility=row.visibility if row is not None else None,  # type: ignore[arg-type]
        description=row.description if row is not None else "",
        source=view.source,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        managed=row is not None and row.litellm_model_id is not None,
        origin=None if row is not None or dep is None else "db" if dep.db_model else "config",
        deployments=len(view.deployments),
        api_base=(dep.api_base or None) if admin and dep is not None else None,
        max_input_tokens=dep.max_input_tokens if dep else None,
        max_output_tokens=dep.max_output_tokens if dep else None,
        input_price=_price(dep.input_cost_per_token) if dep else None,
        output_price=_price(dep.output_cost_per_token) if dep else None,
        grants=grants,
        access=access,  # type: ignore[arg-type]
        created_by=row.created_by if row is not None and admin else None,
        created_at=row.created_at if row is not None else None,
    )


def grant_out(raw: dict[str, Any]) -> GrantOut:
    """用户中心的一条 ACL → 授权行。"""
    subject = raw.get("subject") or {}
    return GrantOut(
        id=str(raw.get("id") or raw.get("acl_id") or ""),
        subject_type=str(raw.get("subject_type") or subject.get("type") or "").lower(),
        subject_id=str(raw.get("subject_id") or subject.get("id") or ""),
        subject_name=str(raw.get("subject_name") or subject.get("name") or ""),
        permission=str(raw.get("permission") or raw.get("level") or ""),
        include_sub=bool(raw.get("include_sub")),
    )


def internal_model_out(view: ModelView) -> InternalModelOut:
    row, dep = view.row, view.primary
    assert row is not None
    return InternalModelOut(
        name=row.name,
        display_name=row.display_name or row.name,
        provider=row.provider,
        max_input_tokens=dep.max_input_tokens if dep else None,
        max_output_tokens=dep.max_output_tokens if dep else None,
    )


def key_out(
    key: GwApiKey,
    *,
    allowed: set[str],
    callable_names: set[str],
    platform_names: set[str],
    secret: str | None = None,
) -> KeySecretOut | KeyOut:
    """allowed：这把 Key 能选的模型；callable_names：平台登记且 LiteLLM 里有的；
    platform_names：平台登记的（含 LiteLLM 中缺失的）。"""

    def state(name: str) -> str:
        if name not in platform_names:
            return "deleted"
        if name not in callable_names:
            return "missing"
        return "ok" if name in allowed else "revoked"

    models = [KeyModelOut(name=m, state=state(m)) for m in key.models or []]  # type: ignore[arg-type]
    effective = [m.name for m in models if m.state == "ok"]
    body = {
        "uuid": key.uuid,
        "key_type": key.key_type,
        "user_uuid": key.user_uuid,
        "client_type": key.client_type,
        "client_code": key.client_code,
        "name": key.name,
        "status": key.status,
        "masked": mask(key.secret),
        "models": models,
        "effective": effective,
        "no_models": key.status != "pending" and not effective,
        "sync_error": key.sync_error,
        "created_by": key.created_by,
        "created_at": key.created_at,
        "rotated_at": key.rotated_at,
    }
    if secret is not None:
        return KeySecretOut(**body, secret=secret)
    return KeyOut(**body)


def model_rows_by_name(rows: list[GwModel]) -> dict[str, GwModel]:
    return {r.name: r for r in rows}
