"""身份注入（文档 §5.2，决策 1：不做鉴权）。

这是**唯一**的身份来源 —— 不允许在 service 层另取。
将来换成 JWT 解析 / SSO 只改这一个函数。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Header

from .config import get_settings


async def current_user(
    x_user_id: Annotated[UUID | None, Header(alias="X-User-Id")] = None,
) -> UUID:
    """MVP：从 header 取，缺失时回落到配置的默认用户。"""
    if x_user_id is not None:
        return x_user_id
    return get_settings().default_user_id
