"""有效权限的计算（权限设计 §06）。

命中的授权 = 授予本人 ∪ 授予所在部门 ∪ 授予上级部门且含下级 —— 角色授权与数据授权同一规则。
有效角色 = 命中的角色授权 ∪ 命中岗位的默认角色（记录每个来源）；已停用用户为空。

★ 首版不加缓存：按索引查询，数万用户规模足够快。需要时再按「用户 + 授权版本号」缓存
  （总体设计 §13），计算入口只有这里，加缓存不影响调用方。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..builtin import SUPER_ROLE_CODE
from ..common import advisory_lock
from ..db.models import (
    App,
    AppScopeDept,
    Dept,
    Grant,
    Menu,
    Operation,
    Position,
    PositionRole,
    Role,
    RoleMenu,
    RoleOperation,
    User,
)
from ..errors import Conflict

__all__ = [
    "RoleSource",
    "authz_snapshot",
    "can_access",
    "dept_chain",
    "effective_roles",
    "expand_subjects",
    "matched_grants",
    "role_holders",
    "subject_filter",
    "super_guard",
    "visible_menu_tree",
]


async def dept_chain(session: AsyncSession, dept_uuid: UUID) -> list[UUID]:
    """部门链：根在前，自己在最后（由物化路径解析）。"""
    path = await session.scalar(select(Dept.path).where(Dept.uuid == dept_uuid))
    if not path:
        return []
    return [UUID(p) for p in path.strip("/").split("/")]


def subject_filter(model: Any, user_uuid: UUID, dept_uuid: UUID, ancestors: Iterable[UUID]):  # type: ignore[no-untyped-def]
    """角色授权与数据授权共用的命中条件。"""
    anc = list(ancestors)
    cond = [model.user_uuid == user_uuid, model.dept_uuid == dept_uuid]
    if anc:
        cond.append(and_(model.include_sub.is_(True), model.dept_uuid.in_(anc)))
    return or_(*cond)


async def matched_grants(session: AsyncSession, user: User) -> list[Grant]:
    chain = await dept_chain(session, user.dept_uuid)
    rows = await session.scalars(
        select(Grant).where(subject_filter(Grant, user.uuid, user.dept_uuid, chain[:-1]))
    )
    return list(rows)


@dataclass
class RoleSource:
    kind: str  # user | dept | position
    label: str
    grant_uuid: UUID


@dataclass
class EffectiveRole:
    role: Role
    sources: list[RoleSource] = field(default_factory=list)


async def _source_label(session: AsyncSession, g: Grant) -> RoleSource:
    if g.subject_type == "user":
        return RoleSource("user", "直接授权", g.uuid)
    name = await session.scalar(select(Dept.name).where(Dept.uuid == g.dept_uuid))
    return RoleSource("dept", f"部门：{name}{'（含下级）' if g.include_sub else ''}", g.uuid)


async def effective_roles(session: AsyncSession, user: User) -> dict[UUID, EffectiveRole]:
    if user.status == "disabled":
        return {}
    grants = await matched_grants(session, user)
    out: dict[UUID, EffectiveRole] = {}
    role_cache: dict[UUID, Role] = {}

    async def role_of(uuid: UUID) -> Role:
        if uuid not in role_cache:
            role_cache[uuid] = (
                await session.execute(select(Role).where(Role.uuid == uuid))
            ).scalar_one()
        return role_cache[uuid]

    for g in grants:
        if g.kind == "role" and g.role_uuid is not None:
            src = await _source_label(session, g)
            out.setdefault(g.role_uuid, EffectiveRole(await role_of(g.role_uuid))).sources.append(
                src
            )
        elif g.kind == "position" and g.position_uuid is not None:
            pos = (
                await session.execute(select(Position).where(Position.uuid == g.position_uuid))
            ).scalar_one()
            role_uuids = await session.scalars(
                select(PositionRole.role_uuid).where(PositionRole.position_uuid == pos.uuid)
            )
            for ru in role_uuids:
                item = out.setdefault(ru, EffectiveRole(await role_of(ru)))
                label = f"岗位：{pos.name}"
                if not any(s.label == label for s in item.sources):
                    item.sources.append(RoleSource("position", label, g.uuid))
    return out


async def can_access(session: AsyncSession, user: User, app: App) -> bool:
    if user.status == "disabled" or app.status != "active":
        return False
    if app.is_builtin or app.scope_all:
        return True
    chain = await dept_chain(session, user.dept_uuid)
    hit = await session.scalar(
        select(AppScopeDept.uuid)
        .where(AppScopeDept.app_uuid == app.uuid, AppScopeDept.dept_uuid.in_(chain))
        .limit(1)
    )
    return hit is not None


async def visible_menu_tree(
    session: AsyncSession, app_uuid: UUID, role_uuids: Iterable[UUID]
) -> tuple[list[dict[str, Any]], set[UUID]]:
    """可见菜单 = 公共菜单 ∪ 角色绑定的菜单，再补上级目录（目录只在有可见子菜单时出现）。"""
    menus = list(
        await session.scalars(
            select(Menu).where(Menu.app_uuid == app_uuid).order_by(Menu.sort, Menu.id)
        )
    )
    by_uuid = {m.uuid: m for m in menus}
    rids = list(role_uuids)
    bound = (
        set(await session.scalars(select(RoleMenu.menu_uuid).where(RoleMenu.role_uuid.in_(rids))))
        if rids
        else set()
    )
    visible: set[UUID] = set()
    for m in menus:
        if m.type == "menu" and (m.is_public or m.uuid in bound):
            visible.add(m.uuid)
            parent = by_uuid.get(m.parent_uuid) if m.parent_uuid else None
            while parent is not None:
                visible.add(parent.uuid)
                parent = by_uuid.get(parent.parent_uuid) if parent.parent_uuid else None

    def build(parent: UUID | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in menus:
            if m.parent_uuid == parent and m.uuid in visible:
                if m.type == "dir":
                    out.append({"name": m.name, "icon": m.icon, "children": build(m.uuid)})
                else:
                    out.append({"code": m.code, "name": m.name, "icon": m.icon, "path": m.path})
        return out

    return build(None), visible


async def authz_snapshot(session: AsyncSession, user: User, app: App) -> dict[str, Any]:
    """授权快照：只含该应用；不在可访问范围内时角色 / 操作码 / 菜单一律为空（应用接入设计 §14）。"""
    access = await can_access(session, user, app)
    roles: list[Role] = []
    if access:
        eff = await effective_roles(session, user)
        roles = [e.role for e in eff.values() if e.role.app_uuid == app.uuid]
    rids = [r.uuid for r in roles]
    perms: list[str] = []
    if rids:
        perms = sorted(
            set(
                await session.scalars(
                    select(Operation.code)
                    .join(RoleOperation, RoleOperation.operation_uuid == Operation.uuid)
                    .where(RoleOperation.role_uuid.in_(rids))
                )
            )
        )
    menus, _ = await visible_menu_tree(session, app.uuid, rids) if access else ([], set())
    return {
        "can_access": access,
        "roles": sorted(r.code for r in roles),
        "permissions": perms,
        "menus": menus,
    }


async def expand_subjects(session: AsyncSession, rows: Iterable[Any]) -> dict[UUID, list[Any]]:
    """把授权（角色授权或数据授权）展开为用户：返回 用户 → 命中的授权行。"""
    out: dict[UUID, list[Any]] = {}
    for r in rows:
        if r.subject_type == "user":
            out.setdefault(r.user_uuid, []).append(r)
            continue
        dept = (await session.execute(select(Dept).where(Dept.uuid == r.dept_uuid))).scalar_one()
        q = select(User.uuid)
        if r.include_sub:
            q = q.join(Dept, Dept.uuid == User.dept_uuid).where(Dept.path.like(dept.path + "%"))
        else:
            q = q.where(User.dept_uuid == dept.uuid)
        for uid in await session.scalars(q):
            out.setdefault(uid, []).append(r)
    return out


async def role_holders(session: AsyncSession, role_uuid: UUID) -> dict[UUID, list[Grant]]:
    """拥有该角色的人（含已停用，调用方按需过滤）：直接授予该角色 + 授予带有它的岗位。"""
    pos_uuids = list(
        await session.scalars(
            select(PositionRole.position_uuid).where(PositionRole.role_uuid == role_uuid)
        )
    )
    cond = [Grant.role_uuid == role_uuid]
    if pos_uuids:
        cond.append(Grant.position_uuid.in_(pos_uuids))
    grants = list(await session.scalars(select(Grant).where(or_(*cond))))
    return await expand_subjects(session, grants)


async def _assert_super_exists(session: AsyncSession) -> None:
    role_uuid = await session.scalar(select(Role.uuid).where(Role.code == SUPER_ROLE_CODE))
    if role_uuid is None:  # 内置角色尚未同步（只在测试的空库里出现）
        return
    holders = await role_holders(session, role_uuid)
    if holders:
        active = await session.scalar(
            select(User.uuid).where(User.uuid.in_(list(holders)), User.status == "active").limit(1)
        )
        if active is not None:
            return
    raise Conflict("LAST_SUPER_ADMIN", "该操作会导致没有可用的超级管理员")


@asynccontextmanager
async def super_guard(session: AsyncSession) -> AsyncIterator[None]:
    """可能减少超级管理员的写操作包在这里：先串行化，变更后校验，失败抛错由请求事务整体回滚。

    ★ 串行化避免「两个管理员同时撤销两位超级管理员，各自检查都通过」（权限设计 §09）。
    """
    await advisory_lock(session, "uc_super_guard")
    yield
    await session.flush()
    await _assert_super_exists(session)
