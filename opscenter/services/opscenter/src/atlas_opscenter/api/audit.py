"""运营审计（只读）：要操作码 ops:audit（内置角色「运营审计员」），看全部模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter

from ..audit import AuditFilter, search
from .deps import AuditorDep, SessionDep

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit(
    _: AuditorDep,
    session: SessionDep,
    action: str | None = None,
    module: str | None = None,
    tenant: UUID | None = None,
    operator: UUID | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = await search(session, AuditFilter(action, module, tenant, operator, before, limit))
    return [
        {
            "id": str(r.audit_uuid),
            "at": r.at.isoformat(),
            "operator_id": str(r.operator_uuid) if r.operator_uuid else None,
            "operator": r.operator_name,
            "action": r.action,
            "module": r.module,
            "target": r.target,
            "tenant_id": str(r.tenant_uuid) if r.tenant_uuid else None,
            "detail": r.detail,
            "result": r.result,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]
