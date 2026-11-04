"""可选的出站凭据档案（c4-model/agent/code-credentials.html「列出可选档案」）。

    GET /credential-profiles    调用者（人，或代人建会话的业务应用）能选哪些档案

★ 只返回 code、注入方式和主机，不含凭据值。建会话的表单、TeamFlow 据此给出选项；建会话时
  再经凭据组件校验一次，之后每轮还会重新授权。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...auth.dependency import current_principal
from ...auth.principal import Principal
from ...credentials import CredentialGrants, CredentialProfileStore
from ...deps import SettingsDep

router = APIRouter(tags=["credentials"])


class CredentialProfileOut(BaseModel):
    code: str
    auth: str
    hosts: list[str]


@router.get("/credential-profiles", response_model=list[CredentialProfileOut])
async def available_profiles(
    settings: SettingsDep, principal: Annotated[Principal, Depends(current_principal)]
) -> list[CredentialProfileOut]:
    grants = CredentialGrants(CredentialProfileStore.from_settings(settings))
    return [
        CredentialProfileOut(code=p.code, auth=p.auth, hosts=list(p.hosts))
        for p in grants.available(str(principal.uid), principal.app or "")
    ]
