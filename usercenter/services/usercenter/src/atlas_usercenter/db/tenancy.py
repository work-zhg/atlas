"""租户上下文与自动过滤（租户设计 §06）。

一个数据库会话在同一时刻只属于一个租户：

    set_tenant(session, tenant_uuid)   之后的查询自动带 tenant_uuid = 该租户；新行自动填它
    platform_scope(session)            显式跨租户（运营接口里管租户、令牌解析、迁移脚本）
    catalog_scope(session)             应用目录（运营接口里管应用、操作、菜单、数据编码、
                                       内置角色）：只看得到应用内置角色，碰任何租户表都报错

★ 过滤挂在 SQLAlchemy 的 do_orm_execute 上，两层：with_loader_criteria 管列里出现的 ORM 实体
  （以及由它们发起的 join）；select(func.count()).select_from(User)、只在 join / 子查询的 FROM
  里出现的租户表它不管（all_mappers 也是空的，没有上下文时连报错都不会），所以再把语句里每个
  SELECT 的 FROM 过一遍：FROM 里的加在 WHERE，join 进来的加在 ON。规则同 atlas_tenancy。
  业务代码不用、也不应该手写 tenant_uuid 条件。
★ 运营接口不进入任何租户（租户隔离设计 §3.5）：管应用目录时在 catalog_scope 里，按结构保证
  读不到租户自定义的角色、授权和人；管租户时只碰租户本身，进租户只为建首个管理员。
★ **没有租户上下文时查询租户表直接报错**，而不是返回全部租户的数据：漏设上下文的代码路径
  在测试里第一次执行就会暴露。
★ 绕过 ORM 的 Core 语句（Table.delete() / text()）不受过滤 —— 只允许用在非租户表上，或
  调用方已经用租户内的主键限定了范围。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import event, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.sql import visitors

from .models import AuditEvent, Base, Role, TenantOwned

__all__ = [
    "MissingTenant",
    "catalog_scope",
    "current_tenant",
    "platform_scope",
    "set_catalog",
    "set_tenant",
    "tenant_scope",
]

_KEY = "uc_tenant"
#: 跨租户：不过滤（显式声明才进入）
_PLATFORM = "platform"
#: 应用目录：只有应用内置角色，租户表一律不许碰
_CATALOG = "catalog"


class MissingTenant(RuntimeError):
    """查询或写入租户表时没有租户上下文 —— 是代码 bug，不是用户错误。"""


def set_tenant(session: AsyncSession | Session, tenant_uuid: UUID) -> None:
    session.info[_KEY] = tenant_uuid


def set_catalog(session: AsyncSession | Session) -> None:
    """这个会话之后只管应用目录（运营接口的一个请求）。见 catalog_scope。"""
    session.info[_KEY] = _CATALOG


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
    """临时跨租户。只用于运营接口管租户、「还不知道租户」的身份解析、迁移脚本。"""
    prev = _swap(session, _PLATFORM)
    try:
        yield
    finally:
        _restore(session, prev)


@contextmanager
def catalog_scope(session: AsyncSession | Session) -> Iterator[None]:
    """管应用目录（运营接口）：角色只看得到应用内置的，碰租户表直接报错。"""
    prev = _swap(session, _CATALOG)
    try:
        yield
    finally:
        _restore(session, prev)


@contextmanager
def tenant_scope(session: AsyncSession | Session, tenant_uuid: UUID) -> Iterator[None]:
    """临时切到某个租户（运营中心建租户时，在新租户里建根部门与管理员）。"""
    prev = _swap(session, tenant_uuid)
    try:
        yield
    finally:
        _restore(session, prev)


def _is_tenant_entity(cls: type) -> bool:
    return issubclass(cls, (TenantOwned, Role))


@cache
def _tenant_tables() -> frozenset[str]:
    """全部租户表（含两级的角色表）。模型在导入时就定了，算一次即可。"""
    return frozenset(
        m.local_table.name for m in Base.registry.mappers if _is_tenant_entity(m.class_)
    )


def _tables_in(statement: Any, names: frozenset[str]) -> set[str]:
    """语句里任何位置（FROM、join、子查询）出现的租户表。"""
    keep = set(getattr(statement, "_with_options", ()))
    return {
        elem.name
        for elem in visitors.iterate(statement, {"stop_on": keep})
        if isinstance(elem, sa.Table) and elem.name in names
    }


def _condition(table: Any, name: str, scope: Any) -> Any:
    col = table.c.tenant_uuid
    if name != Role.__tablename__:
        return col == scope
    # 角色两级：应用内置（无租户）+ 本租户自定义；应用目录里只有内置的
    return col.is_(None) if scope == _CATALOG else or_(col.is_(None), col == scope)


def _tenant_from(fc: Any, names: frozenset[str]) -> tuple[Any, str] | None:
    """一个 FROM 元素如果是租户表（或它的别名），返回 (能取到 tenant_uuid 列的元素, 表名)。"""
    base = fc
    while not isinstance(base, sa.Table):
        inner = getattr(base, "element", None)
        if inner is None or isinstance(inner, sa.Select):
            return None  # 子查询本身不是表：它里面的 SELECT 会单独处理
        base = inner
    return (fc, base.name) if base.name in names else None


def _leftmost(fc: Any) -> Any:
    while isinstance(fc, sa.Join):
        fc = fc.left
    return fc


def _scope_every_from(statement: Any, scope: Any, names: frozenset[str]) -> Any:
    def visit_select(select: Any) -> None:
        for fc in select.get_final_froms():
            hit = _tenant_from(_leftmost(fc), names)
            if hit is not None:
                select._where_criteria += (_condition(*hit, scope),)

    def visit_join(join: Any) -> None:
        hit = _tenant_from(join.right, names)
        if hit is not None:
            join.onclause = sa.and_(join.onclause, _condition(*hit, scope))

    keep = set(getattr(statement, "_with_options", ()))
    return visitors.cloned_traverse(
        statement, {"stop_on": keep}, {"select": visit_select, "join": visit_join}
    )


@event.listens_for(Session, "do_orm_execute")
def _filter_by_tenant(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    # 懒加载的列、关系沿用发起加载时的条件，不再重复判断
    if state.is_column_load or state.is_relationship_load:
        return
    scope = state.session.info.get(_KEY)
    if scope == _PLATFORM:
        return
    names = _tenant_tables()
    touched = {
        m.class_.__tablename__ for m in state.all_mappers if _is_tenant_entity(m.class_)
    } | _tables_in(state.statement, names)
    if not touched:
        return
    if scope == _CATALOG:
        owned = sorted(touched - {Role.__tablename__})
        if owned:
            raise MissingTenant(f"应用目录里不能查租户表 {owned}")
    elif not isinstance(scope, UUID):
        raise MissingTenant(f"查询租户表 {sorted(touched)} 时没有租户上下文")
    statement = state.statement
    if state.is_select:
        statement = _scope_every_from(statement, scope, names)
    # ★ 两种范围用两个 lambda：SQLAlchemy 按 lambda 的代码缓存 SQL 结构，lambda 里按闭包变量
    #   分支会沿用第一次的结构（应用目录范围曾被当成租户范围，把 'catalog' 当租户 uuid 绑进去）
    if scope == _CATALOG:
        options = [
            with_loader_criteria(Role, lambda cls: cls.tenant_uuid.is_(None), include_aliases=True)
        ]
    else:
        # 角色两级：应用内置（无租户）+ 本租户自定义
        options = [
            with_loader_criteria(
                Role,
                lambda cls: or_(cls.tenant_uuid.is_(None), cls.tenant_uuid == scope),
                include_aliases=True,
            )
        ]
        options.append(
            with_loader_criteria(
                TenantOwned, lambda cls: cls.tenant_uuid == scope, include_aliases=True
            )
        )
    state.statement = statement.options(*options)


@event.listens_for(Session, "before_flush")
def _fill_tenant(session: Session, _ctx: Any, _instances: Any) -> None:
    scope = session.info.get(_KEY)
    for obj in session.new:
        if scope == _CATALOG and (
            isinstance(obj, TenantOwned) or (isinstance(obj, Role) and obj.tenant_uuid is not None)
        ):
            raise MissingTenant(f"应用目录里不能写租户数据 {type(obj).__name__}")
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
