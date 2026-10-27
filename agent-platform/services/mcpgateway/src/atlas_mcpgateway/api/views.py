"""库里的行 → 接口里的形状。"""

from __future__ import annotations

from collections import Counter

from ..db.models import McpApiKey, McpService
from ..definitions import model_name, read_only
from ..keys import mask
from ..registry import ToolView
from .schemas import (
    AccessCheckOut,
    KeyOut,
    MetadataServiceOut,
    ReviewOut,
    ServiceDetailOut,
    ServiceOut,
    ToolOut,
)


def tool_out(service: str, view: ToolView) -> ToolOut:
    t = view.row
    review = view.review
    return ToolOut(
        name=t.name,
        model_name=model_name(service, t.name),
        description=t.description,
        input_schema=t.input_schema,
        annotations=t.annotations,
        read_only=read_only(t.annotations),
        digest=t.digest,
        issues=list(t.issues),
        status=view.status,
        changed_at=t.changed_at,
        previous=t.previous,
        review=ReviewOut(
            decision=review.decision,
            comment=review.comment,
            reviewed_by=review.reviewed_by,
            reviewed_at=review.updated_at,
        )
        if review
        else None,
    )


def service_out(svc: McpService, tools: list[ToolView]) -> ServiceOut:
    counts = Counter(v.status for v in tools if v.row.removed_at is None)
    return ServiceOut(
        name=svc.name,
        display_name=svc.display_name,
        description=svc.description,
        upstream_kind=svc.upstream_kind,
        upstream_name=svc.upstream_name,
        credential_mode=svc.credential_mode,
        review_required=svc.review_required,
        call_timeout_s=svc.call_timeout_s,
        enabled=svc.status == "enabled",
        created_by=svc.created_by,
        updated_by=svc.updated_by,
        created_at=svc.created_at,
        updated_at=svc.updated_at,
        fetched_at=svc.fetched_at,
        fetch_error=svc.fetch_error,
        changed_at=svc.changed_at,
        content_hash=svc.content_hash,
        access_check=AccessCheckOut(
            ok=svc.access_check_ok,
            detail=svc.access_check_detail,
            checked_at=svc.access_checked_at,
        ),
        tool_counts=dict(counts),
    )


def service_detail(svc: McpService, tools: list[ToolView]) -> ServiceDetailOut:
    current = [v for v in tools if v.row.removed_at is None]
    return ServiceDetailOut(
        **service_out(svc, tools).model_dump(),
        tools=[tool_out(svc.name, v) for v in current],
        removed_tools=sorted(v.row.name for v in tools if v.row.removed_at is not None),
    )


def metadata_out(svc: McpService, tools: list[ToolView]) -> MetadataServiceOut:
    current = [v for v in tools if v.row.removed_at is None]
    return MetadataServiceOut(
        name=svc.name,
        display_name=svc.display_name,
        description=svc.description,
        enabled=svc.status == "enabled",
        review_required=svc.review_required,
        call_timeout_s=svc.call_timeout_s,
        credential_mode=svc.credential_mode,
        fetched_at=svc.fetched_at,
        fetch_error=svc.fetch_error,
        changed_at=svc.changed_at,
        content_hash=svc.content_hash,
        tools=[tool_out(svc.name, v) for v in current],
        removed_tools=sorted(v.row.name for v in tools if v.row.removed_at is not None),
    )


def key_out(row: McpApiKey) -> KeyOut:
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
