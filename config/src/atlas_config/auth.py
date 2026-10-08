"""认证（设计 §14）。

两条通道、两种凭据，互不通用：

  /config/*    用户。规则与运行时一致：dev 模式读 X-User-Id，没给就用默认用户，
               角色固定 admin。jwt 模式等用户模块上线后接（同一组测试向量）。
  /internal/*  运行时进程。共享令牌 ATLAS_CONFIG_INTERNAL_TOKEN；**不接受用户身份**
               —— 内部接口能读到全部已发布内容，不能让浏览器直接打到它。

★ 与运行时各自实现，不抽公共包：规则只有几十行，抽出来就是第三个所有服务都
  依赖的包，收益抵不上耦合。
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from .settings import ConfigSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["Principal", "current_principal", "require_admin", "require_builder", "require_internal"]


@dataclass(frozen=True)
class Principal:
    user: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has(self, role: str) -> bool:
        # admin ⊃ builder ⊃ member
        order = {"member": 0, "builder": 1, "admin": 2}
        need = order[role]
        return any(order.get(r, -1) >= need for r in self.roles)


def current_principal(
    settings: Annotated[ConfigSettings, Depends(get_settings)],
    x_user_id: Annotated[str | None, Header()] = None,
) -> Principal:
    user = (x_user_id or "").strip() or settings.default_user
    return Principal(user=user, roles=frozenset({"admin"}))


def _require(role: str):  # type: ignore[no-untyped-def]
    def dep(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.has(role):
            raise HTTPException(status_code=403, detail=f"需要 {role} 角色")
        return principal

    return dep


require_builder = _require("builder")
require_admin = _require("admin")


def require_internal(
    settings: Annotated[ConfigSettings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.internal_token.get_secret_value() if settings.internal_token else ""
    if not expected:
        return  # 本机开发：启动时已告警（__main__）
    given = (authorization or "").removeprefix("Bearer ").strip()
    # ★ 定时比较：令牌比对不能因前缀匹配长度而泄露信息
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="内部接口需要服务间令牌")
