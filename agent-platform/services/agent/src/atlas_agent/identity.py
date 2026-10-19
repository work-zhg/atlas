"""身份注入（权限设计 §4；内网身份传递 §3）。

这是**唯一**的身份来源 —— 不允许在 service 层另取。主体由 auth/dependency.py 按请求头解析；
这里给只关心「谁在用」的接口一个 UUID。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from .auth.dependency import current_principal
from .auth.principal import Principal


async def current_user(principal: Annotated[Principal, Depends(current_principal)]) -> UUID:
    """谁在用（X-Uid）：会话、记忆按它归属。"""
    return principal.uid
