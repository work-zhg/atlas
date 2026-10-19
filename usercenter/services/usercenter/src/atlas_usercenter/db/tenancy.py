"""租户上下文与自动过滤（租户设计 §06）。

一个数据库会话在同一时刻只属于一个租户：

    set_tenant(session, tenant_uuid)   之后的查询自动带 tenant_uuid = 该租户；新行自动填它
    platform_scope(session)            显式跨租户（平台级操作、令牌解析、迁移脚本）

★ 过滤挂在 SQLAlchemy 的 do_orm_execute 上（with_loader_criteria），所以 select / ORM update /
  ORM delete 里出现的每个租户实体 —— 含 join、子查询 —— 都会被加上租户条件。业务代码
  不用、也不应该手写 tenant_uuid 条件。
★ **没有租户上下文时查询租户表直接报错**，而不是返回全部租户的数据：漏设上下文的代码路径
  在测试里第一次执行就会暴露。
★ 绕过 ORM 的 Core 语句（Table.delete() / text()）不受过滤 —— 只允许用在非租户表上，或
  调用方已经用租户内的主键限定了范围。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import event, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from .models import AuditEvent, Role, TenantOwned

__all__ = [
    "MissingTenant",
    "current_tenant",
    "platform_scope",
    "set_tenant",
    "tenant_scope",
]

_KEY = "uc_tenant"
#: 平台级：不过滤（显式声明才进入）
_PLATFORM = "platform"


class MissingTenant(RuntimeError):
    """查询或写入租户表时没有租户上下文 —— 是代码 bug，不是用户错误。"""


def set_tenant(session: AsyncSession | Session, tenant_uuid: UUID) -> None:
    session.info[_KEY] = tenant_uuid


def current_tenant(session: AsyncSession | Session) -> UUID | None:
    value = session.info.get(_KEY)
    return value if isinstance(value, UUID) else None


def _swap(session: AsyncSession | Session, value: Any) -> Any:
    prev = session.info.get(_KEY)
    session.info[_KEY] = value
    return prev


def _restore(session: AsyncSession | Session, prev: Any) -> None:
    if prev is None:
        session.info.pop(_KEY, None)
    else:
        session.info[_KEY] = prev


@contextmanager
def platform_scope(session: AsyncSession | Session) -> Iterator[None]:
    """临时跨租户。只用于平台级操作与「还不知道租户」的身份解析。"""
    prev = _swap(session, _PLATFORM)
    try:
        yield
    finally:
        _restore(session, prev)


@contextmanager
def tenant_scope(session: AsyncSession | Session, tenant_uuid: UUID) -> Iterator[None]:
    """临时切到某个租户（平台管理员建租户时，在新租户里建根部门与管理员）。"""
    prev = _swap(session, tenant_uuid)
    try:
        yield
    finally:
        _restore(session, prev)


def _is_tenant_entity(cls: type) -> bool:
    return issubclass(cls, (TenantOwned, Role))


@event.listens_for(Session, "do_orm_execute")
def _filter_by_tenant(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    scope = state.session.info.get(_KEY)
    if scope == _PLATFORM:
        return
    if not isinstance(scope, UUID):
        touched = [m.class_.__name__ for m in state.all_mappers if _is_tenant_entity(m.class_)]
        if touched:
            raise MissingTenant(f"查询租户表 {touched} 时没有租户上下文")
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantOwned,
            lambda cls: cls.tenant_uuid == scope,
            include_aliases=True,
        ),
        # 角色两级：应用内置（无租户）+ 本租户自定义
        with_loader_criteria(
            Role,
            lambda cls: or_(cls.tenant_uuid.is_(None), cls.tenant_uuid == scope),
            include_aliases=True,
        ),
    )


@event.listens_for(Session, "before_flush")
def _fill_tenant(session: Session, _ctx: Any, _instances: Any) -> None:
    scope = session.info.get(_KEY)
    for obj in session.new:
        if isinstance(obj, TenantOwned):
            if obj.tenant_uuid is None:
                if not isinstance(scope, UUID):
                    raise MissingTenant(f"写入 {type(obj).__name__} 时没有租户上下文")
                obj.tenant_uuid = scope
            elif isinstance(scope, UUID) and obj.tenant_uuid != scope:
                # 纵深防御：租户上下文里写别的租户的行一律拒绝（复合外键之外的第二道）
                raise MissingTenant(f"在租户 {scope} 的上下文里写入别的租户的 {type(obj).__name__}")
        elif isinstance(obj, AuditEvent) and obj.tenant_uuid is None and isinstance(scope, UUID):
            obj.tenant_uuid = scope
