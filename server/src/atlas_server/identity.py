"""身份注入（权限设计 §4）。

这是**唯一**的身份来源 —— 不允许在 service 层另取。主体由 auth/dependency.py 解析
（服务账号密钥 / 用户中心会话 / dev 请求头）；这里给只关心「所有者是谁」的接口一个 UUID。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from .auth.dependency import current_principal
from .auth.principal import Principal


async def current_user(principal: Annotated[Principal, Depends(current_principal)]) -> UUID:
    """资源所有者 id：用户 = 用户中心用户 id；服务账号 = 服务账号 id。"""
    return principal.owner_id
