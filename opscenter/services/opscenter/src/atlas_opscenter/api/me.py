"""当前运营人员：控制台据此只显示有级别的模块，按钮按级别置灰（不隐藏）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..access import AUDIT_OP
from .deps import CatalogDep, PrincipalDep

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
async def me(p: PrincipalDep, catalog: CatalogDep) -> dict[str, Any]:
    return {
        "user": {"id": str(p.user_uuid), "name": p.name, "tenant_id": str(p.tenant_uuid)},
        "modules": [
            {"code": m.code, "name": m.name, "system": m.system, "level": p.level(m).name}
            for m in catalog.all()
        ],
        "can_audit": AUDIT_OP in p.operations,
    }
