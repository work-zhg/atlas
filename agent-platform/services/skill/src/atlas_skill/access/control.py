"""AccessControl：管理接口的调用身份和操作码（c4-model/skill/code-access.html）。只给入口用。

    X-Uid         谁在用（网关按登录态写入）：上传人、审查人、审计里的操作人
    X-Auth-Uid    按谁的权限判，缺省 = X-Uid
    X-Tenant-Id   租户（必填）：带了用户的以它向用户中心取快照（找不到这个人就是 401），然后进入它

★ 四眼原则这类和具体某个上传有关的规则不在这里（技能版本的 ReviewPolicy）。
★ 操作码由运营人员在运营中心录入「技能」应用名下（见 README「用户中心登记」）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from ..errors import Unauthorized
from .usercenter import UserCenterGateway

__all__ = [
    "AUDIT_VIEW",
    "SKILL_REVIEW",
    "SKILL_UPLOAD",
    "AccessControl",
    "Principal",
]

#: 上传
SKILL_UPLOAD = "skill:upload"
#: 审查、停用 / 启用、下架、重新检查、从技能市场安装
SKILL_REVIEW = "skill:review"
#: 查看审计
AUDIT_VIEW = "audit:view"

_FORWARD = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}


@dataclass(frozen=True)
class Principal:
    #: 谁在用（X-Uid）：上传人、审查人
    user: str
    operations: frozenset[str] = field(default_factory=frozenset)

    def has_any(self, *ops: str) -> bool:
        return any(op in self.operations for op in ops)


class AccessControl:
    def __init__(self, uc: UserCenterGateway) -> None:
        self._uc = uc

    async def principal(self, headers: Mapping[str, str], tenant: UUID) -> Principal | None:
        """请求头里的人在这个租户里的身份和操作码。没带 X-Uid → None（应用替自己的租户调）。"""
        uid = (headers.get("x-uid") or "").strip()
        if not uid:
            return None
        auth_uid = (headers.get("x-auth-uid") or "").strip() or uid
        identity = {out: headers[name] for name, out in _FORWARD.items() if headers.get(name)}
        snap = await self._uc.authz(auth_uid, tenant, identity)
        if snap.tenant is not None and snap.tenant != tenant:
            raise Unauthorized("请求头里的租户和用户所属的不一致")
        return Principal(user=uid, operations=snap.operations)
