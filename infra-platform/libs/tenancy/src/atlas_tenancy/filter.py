"""自动过滤：挂在 SQLAlchemy 的会话事件上，业务代码不写、也不应该写租户条件。

    查询、ORM 更新和删除   出现的每个租户实体（含连表、子查询）自动加 tenant_uuid = 当前租户
    flush 新行             自动填上当前租户；在 A 的上下文里写 B 的行 → 拒绝；不允许改已有行的租户
    没有上下文             碰到租户表直接报错（TenantViolation.MISSING），而不是返回全部租户的数据
    系统范围               不过滤（TenantScope.system 显式进入）

★ 规则和用户中心 db/tenancy.py 一样，区别只在上下文放在 contextvar 里。
★ 两层，缺一不可：with_loader_criteria 只管列里出现的 ORM 实体（以及由它们发起的 join、关系
  加载），select(func.count()).select_from(Agent)、只在 join / 子查询的 FROM 里出现的租户表
  它都不管（2.0.52 实测）；所以再把语句里每个 SELECT 的 FROM 过一遍，租户表一律加条件：
  FROM 里的加在 WHERE，join 进来的加在 ON（外连接的语义不变）。条件重复加一次没有坏处。
★ 绕过 ORM 的语句（text()、Core 的 Table.delete()）不受过滤：业务代码里不要对租户表这样写，
  只允许在本库的删除、迁移里出现。
★ 导入本包即生效（挂在 Session 类上，异步会话里包着的也是它）：宿主不用另外安装，也就不会忘。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.sql import visitors

from .context import SystemReason, TenantViolation, raw_scope
from .model import TenantOwned

__all__: list[str] = []


def _tenant_entities(state: ORMExecuteState) -> list[str]:
    return [m.class_.__name__ for m in state.all_mappers if issubclass(m.class_, TenantOwned)]


def _tenant_table_names() -> frozenset[str]:
    """全部租户表的全名。TenantOwned 的子类随时可能新增（测试里动态建），每次现算，很便宜。"""
    names: set[str] = set()
    stack = list(TenantOwned.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        if table is not None:
            names.add(table.fullname)
    return frozenset(names)


def _tenant_tables_in(statement: Any, names: frozenset[str]) -> set[str]:
    """语句里任何位置（FROM、join、子查询）出现的租户表。"""
    keep = set(getattr(statement, "_with_options", ()))
    found: set[str] = set()
    for elem in visitors.iterate(statement, {"stop_on": keep}):
        if isinstance(elem, sa.Table) and elem.fullname in names:
            found.add(elem.fullname)
    return found


def _tenant_from(fc: Any, names: frozenset[str]) -> Any | None:
    """一个 FROM 元素如果是租户表（或它的别名），返回能取到 tenant_uuid 列的那个元素。"""
    base = fc
    while not isinstance(base, sa.Table):
        inner = getattr(base, "element", None)
        if inner is None or isinstance(inner, sa.Select):
            return None  # 子查询本身不是表：它里面的 SELECT 会单独处理
        base = inner
    return fc if base.fullname in names else None


def _leftmost(fc: Any) -> Any:
    while isinstance(fc, sa.Join):
        fc = fc.left
    return fc


def _scope_every_from(statement: Any, scope: UUID, names: frozenset[str]) -> Any:
    def visit_select(select: Any) -> None:
        for fc in select.get_final_froms():
            hit = _tenant_from(_leftmost(fc), names)
            if hit is not None:
                select._where_criteria += (hit.c.tenant_uuid == scope,)

    def visit_join(join: Any) -> None:
        hit = _tenant_from(join.right, names)
        if hit is not None:
            join.onclause = sa.and_(join.onclause, hit.c.tenant_uuid == scope)

    # 语句上挂的 option（加载策略等）不是 FROM，也不一定能复制：原样保留，不进遍历
    keep = set(getattr(statement, "_with_options", ()))
    return visitors.cloned_traverse(
        statement, {"stop_on": keep}, {"select": visit_select, "join": visit_join}
    )


@event.listens_for(Session, "do_orm_execute")
def _filter_by_tenant(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    # 懒加载的列、关系沿用发起加载时的条件（with_loader_criteria 会传下去），不再重复判断
    if state.is_column_load or state.is_relationship_load:
        return
    scope = raw_scope()
    if isinstance(scope, SystemReason):
        return
    names = _tenant_table_names()
    if not names:
        return  # 本进程没有租户表（如用户中心只为运营接口导入了本库）：不用遍历语句
    # 只看 all_mappers 不够：select(func.count()).select_from(Agent) 的列里没有 ORM 实体，
    # all_mappers 是空的 —— 不加这一步，它既不被过滤，没有上下文时也不报错
    touched = sorted(set(_tenant_entities(state)) | _tenant_tables_in(state.statement, names))
    if not touched:
        return
    if not isinstance(scope, UUID):
        raise TenantViolation(TenantViolation.MISSING, f"查询租户表 {touched} 时没有租户上下文")
    statement = state.statement
    if state.is_select:
        statement = _scope_every_from(statement, scope, names)
    state.statement = statement.options(
        with_loader_criteria(
            TenantOwned,
            lambda cls: cls.tenant_uuid == scope,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _fill_tenant(session: Session, _ctx: Any, _instances: Any) -> None:
    scope = raw_scope()
    for obj in session.new:
        if not isinstance(obj, TenantOwned):
            continue
        if obj.tenant_uuid is None:
            if not isinstance(scope, UUID):
                # 系统范围里也不替你猜租户：要写就显式给出
                raise TenantViolation(
                    TenantViolation.MISSING, f"写入 {type(obj).__name__} 时没有租户上下文"
                )
            obj.tenant_uuid = scope
        elif isinstance(scope, UUID) and obj.tenant_uuid != scope:
            raise TenantViolation(
                TenantViolation.CROSS_TENANT,
                f"在租户 {scope} 的上下文里写入别的租户的 {type(obj).__name__}",
            )
    for obj in session.dirty:
        if isinstance(obj, TenantOwned) and sa.inspect(obj).attrs.tenant_uuid.history.has_changes():
            raise TenantViolation(
                TenantViolation.CROSS_TENANT, f"{type(obj).__name__} 的租户建好后不能改"
            )
