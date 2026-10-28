"""作用域的配置（§3.2）：编码、描述、关联主键的字段、是否校验数据权限。

管理员经作用域管理接口（要 memory_scope:manage）或命令行（python -m atlas_memory scope …）配置。

★ 作用域之间没有关联关系：需要「人 × 项目」的数据，就单独配一个字段为 uid + project 的作用域，
  而不是让两个作用域互相引用。
★ 关联主键的字段建好后不能改：改了，已有实例的关联主键就对不上了。
★ 打开数据权限开关要先有用户中心的数据编码：开关打开后，运行一次 register-uc 把编码登记到
  用户中心（只有用户中心管理员能建数据编码，本服务的开放接口调用做不到）。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Scope
from .errors import Conflict, Invalid, NotFound
from .keys import validate_fields

__all__ = ["ScopeService", "default_data_code", "scope_view"]

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DATA_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def default_data_code(code: str) -> str:
    """user_project → MemoryUserProject。"""
    return "Memory" + "".join(part.capitalize() for part in code.split("_"))


def scope_view(s: Scope) -> dict[str, Any]:
    return {
        "code": s.code,
        "description": s.description,
        "key_fields": list(s.key_fields),
        "check_permission": s.check_permission,
        "data_code": s.data_code,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


class ScopeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[Scope]:
        return list((await self.session.scalars(select(Scope).order_by(Scope.id))).all())

    async def find(self, code: str) -> Scope | None:
        return await self.session.scalar(select(Scope).where(Scope.code == code))

    async def get(self, code: str) -> Scope:
        row = await self.find(code)
        if row is None:
            raise NotFound(f"作用域 {code} 不存在", code="SCOPE_NOT_FOUND")
        return row

    async def create(
        self,
        *,
        code: str,
        description: str,
        key_fields: list[str],
        check_permission: bool,
        data_code: str | None,
        actor: str,
    ) -> Scope:
        if not _CODE_RE.match(code):
            raise Invalid("作用域编码：小写字母开头，只含小写字母、数字、下划线，最长 32")
        if await self.session.scalar(select(Scope.id).where(Scope.code == code)) is not None:
            raise Conflict(f"作用域 {code} 已存在", code="SCOPE_EXISTS")
        row = Scope(
            code=code,
            description=description.strip(),
            key_fields=validate_fields(key_fields),
            check_permission=check_permission,
            data_code=_data_code(code, data_code, check_permission),
            created_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(
        self,
        code: str,
        *,
        description: str | None = None,
        check_permission: bool | None = None,
        data_code: str | None = None,
    ) -> Scope:
        row = await self.get(code)
        if description is not None:
            row.description = description.strip()
        if check_permission is not None:
            row.check_permission = check_permission
        if data_code is not None or (row.check_permission and not row.data_code):
            row.data_code = _data_code(code, data_code or row.data_code, row.check_permission)
        await self.session.flush()
        return row


def _data_code(code: str, given: str | None, check: bool) -> str | None:
    value = (given or "").strip() or (default_data_code(code) if check else "")
    if not value:
        return None
    if not _DATA_CODE_RE.match(value):
        raise Invalid("数据编码：字母开头，只含字母、数字、下划线，最长 64")
    return value
