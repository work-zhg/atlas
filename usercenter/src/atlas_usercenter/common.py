"""跨模块的小工具：校验正则、分页、展示状态。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Credential, User
from .db.types import utcnow
from .errors import Invalid

__all__ = [
    "ACCOUNT_RE",
    "EMAIL_RE",
    "PHONE_RE",
    "UNSET",
    "Page",
    "advisory_lock",
    "display_status",
    "iso",
    "require_text",
]

ACCOUNT_RE = re.compile(r"^[a-z0-9._]{3,32}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^1\d{10}$")


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


#: 「未传」与「传了 null」要区分（如 PATCH 负责人：null = 清空）
UNSET: Any = _Unset()


@dataclass
class Page:
    page: int = 1
    size: int = 20

    @property
    def offset(self) -> int:
        return (max(self.page, 1) - 1) * self.limit

    @property
    def limit(self) -> int:
        return min(max(self.size, 1), 200)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def require_text(value: str | None, field: str, label: str, max_len: int) -> str:
    v = (value or "").strip()
    if not v:
        raise Invalid("VALIDATION_FAILED", f"请填写{label}", field=field)
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符", field=field)
    return v


def display_status(user: User, cred: Credential | None) -> str:
    """展示状态按优先级推导（用户设计 §07）：停用 > 锁定 > 未激活 > 正常。"""
    if user.status == "disabled":
        return "disabled"
    if cred is not None and cred.locked_until is not None and cred.locked_until > utcnow():
        return "locked"
    if user.activated_at is None:
        return "pending"
    return "active"


async def advisory_lock(session: AsyncSession, key: str) -> None:
    """事务级咨询锁：树结构变更、超级管理员保护等低频写操作串行化。"""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
