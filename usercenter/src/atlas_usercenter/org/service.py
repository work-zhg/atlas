"""组织管理（组织设计）。

规则摘要：唯一根部门；最多 10 级；同级不重名；只删空部门；移动连同子树；
负责人每部门最多一名、须为本部门子树内未停用的用户，脱离子树时自动清空；
直属上级不存储，沿部门链向上取第一个「不是本人、未停用」的负责人。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..common import UNSET, Page, advisory_lock, require_text
from ..db.models import App, AppScopeDept, DataAcl, Dept, Grant, User
from ..errors import Conflict, Invalid, NotFound
from ..ids import uuid7

__all__ = ["MAX_DEPTH", "OrgService"]

MAX_DEPTH = 10


def _check_name(name: str | None) -> str:
    v = require_text(name, "name", "部门名称", 50)
    if "/" in v:
        raise Invalid("VALIDATION_FAILED", "部门名称不能包含「/」", field="name")
    return v


class OrgService:
    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    # ───────────────────────────── 读取

    async def get(self, uuid: UUID) -> Dept:
        dept = (
            await self.session.execute(select(Dept).where(Dept.uuid == uuid))
        ).scalar_one_or_none()
        if dept is None:
            raise NotFound("DEPT_NOT_FOUND", "部门不存在")
        return dept

    async def root(self) -> Dept | None:
        return (
            await self.session.execute(select(Dept).where(Dept.parent_uuid.is_(None)))
        ).scalar_one_or_none()

    async def all_depts(self) -> list[Dept]:
        return list(
            await self.session.scalars(select(Dept).order_by(Dept.depth, Dept.sort, Dept.name))
        )

    async def path_names(self, dept: Dept) -> list[str]:
        ids = [UUID(p) for p in dept.path.strip("/").split("/")]
        rows = {
            d.uuid: d.name
            for d in await self.session.scalars(select(Dept).where(Dept.uuid.in_(ids)))
        }
        return [rows[i] for i in ids if i in rows]

    async def tree(self) -> list[dict[str, Any]]:
        depts = await self.all_depts()
        direct = dict(
            (
                await self.session.execute(
                    select(User.dept_uuid, func.count()).group_by(User.dept_uuid)
                )
            ).all()
        )
        leaders = {
            u.uuid: u
            for u in await self.session.scalars(
                select(User).where(User.uuid.in_([d.leader_uuid for d in depts if d.leader_uuid]))
            )
        }
        out = []
        for d in depts:
            total = sum(
                n for dd in depts if dd.path.startswith(d.path) for n in [direct.get(dd.uuid, 0)]
            )
            leader = leaders.get(d.leader_uuid) if d.leader_uuid else None
            out.append(
                {
                    "id": str(d.uuid),
                    "parent_id": str(d.parent_uuid) if d.parent_uuid else None,
                    "name": d.name,
                    "sort": d.sort,
                    "depth": d.depth,
                    "leader": _user_brief(leader),
                    "direct_count": direct.get(d.uuid, 0),
                    "total_count": total,
                }
            )
        return out

    async def detail(self, uuid: UUID) -> dict[str, Any]:
        d = await self.get(uuid)
        leader = await self._user(d.leader_uuid) if d.leader_uuid else None
        children = list(
            await self.session.scalars(
                select(Dept).where(Dept.parent_uuid == d.uuid).order_by(Dept.sort, Dept.name)
            )
        )
        direct = await self.session.scalar(
            select(func.count()).select_from(User).where(User.dept_uuid == d.uuid)
        )
        total = await self.session.scalar(
            select(func.count())
            .select_from(User)
            .join(Dept, Dept.uuid == User.dept_uuid)
            .where(Dept.path.like(d.path + "%"))
        )
        return {
            "id": str(d.uuid),
            "parent_id": str(d.parent_uuid) if d.parent_uuid else None,
            "name": d.name,
            "path": await self.path_names(d),
            "depth": d.depth,
            "leader": _user_brief(leader),
            "direct_count": direct,
            "total_count": total,
            "children": [{"id": str(c.uuid), "name": c.name} for c in children],
            "version": d.version,
        }

    async def subtree_uuids(self, dept: Dept) -> list[UUID]:
        return list(
            await self.session.scalars(select(Dept.uuid).where(Dept.path.like(dept.path + "%")))
        )

    async def in_subtree(self, dept_uuid: UUID, root: Dept) -> bool:
        path = await self.session.scalar(select(Dept.path).where(Dept.uuid == dept_uuid))
        return bool(path and path.startswith(root.path))

    async def members(
        self, uuid: UUID, *, include_sub: bool, q: str | None, page: Page
    ) -> dict[str, Any]:
        d = await self.get(uuid)
        stmt = select(User)
        if include_sub:
            stmt = stmt.join(Dept, Dept.uuid == User.dept_uuid).where(Dept.path.like(d.path + "%"))
        else:
            stmt = stmt.where(User.dept_uuid == d.uuid)
        if q:
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(or_(func.lower(User.name).like(like), User.account.like(like)))
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        users = list(
            await self.session.scalars(stmt.order_by(User.id).offset(page.offset).limit(page.limit))
        )
        return {"total": total, "items": users, "leader_uuid": d.leader_uuid}

    async def _user(self, uuid: UUID | None) -> User | None:
        if uuid is None:
            return None
        return (
            await self.session.execute(select(User).where(User.uuid == uuid))
        ).scalar_one_or_none()

    # ───────────────────────────── 直属上级

    async def manager_of(self, user: User) -> tuple[User | None, str | None]:
        """返回 (直属上级, 来源 own_dept | ancestor)。根部门负责人没有直属上级。"""
        ids = [UUID(p) for p in (await self.get(user.dept_uuid)).path.strip("/").split("/")]
        depts = {
            d.uuid: d for d in await self.session.scalars(select(Dept).where(Dept.uuid.in_(ids)))
        }
        for i, dept_uuid in enumerate(reversed(ids)):
            leader_uuid = depts[dept_uuid].leader_uuid
            if leader_uuid is None or leader_uuid == user.uuid:
                continue
            leader = await self._user(leader_uuid)
            if leader is None or leader.status == "disabled":
                continue
            return leader, "own_dept" if i == 0 else "ancestor"
        return None, None

    # ───────────────────────────── 负责人约束

    async def _check_leader(self, dept: Dept, leader_uuid: UUID) -> None:
        leader = await self._user(leader_uuid)
        if leader is None:
            raise Invalid("SUBJECT_NOT_FOUND", "用户不存在")
        if leader.status == "disabled":
            raise Invalid("LEADER_DISABLED", "不能把已停用的用户设为负责人")
        if not await self.in_subtree(leader.dept_uuid, dept):
            raise Invalid("LEADER_NOT_IN_DEPT", "负责人必须是本部门或其下级部门的成员")

    async def fix_leaders(self) -> list[str]:
        """负责人不再位于部门子树内的，自动清空（组织设计 §07）。返回被清空的部门名。"""
        ud = Dept.__table__.alias("ud")
        rows = (
            await self.session.execute(
                select(Dept, User)
                .join(User, User.uuid == Dept.leader_uuid)
                .join(ud, ud.c.uuid == User.dept_uuid)
                .where(~ud.c.path.like(Dept.path + "%"))
            )
        ).all()
        cleared = []
        for dept, leader in rows:
            dept.leader_uuid = None
            dept.version += 1
            record(
                self.session,
                Actor.system(),
                "dept.leader_clear",
                "dept",
                dept.uuid,
                dept.name,
                reason=f"{leader.name} 已不在该部门及其下级部门",
            )
            cleared.append(dept.name)
        await self.session.flush()
        return cleared

    # ───────────────────────────── 写操作

    async def create(self, parent_uuid: UUID, name: str) -> Dept:
        await advisory_lock(self.session, "uc_dept_tree")
        parent = await self.get(parent_uuid)
        name = _check_name(name)
        if parent.depth >= MAX_DEPTH:
            raise Invalid("DEPTH_EXCEEDED", f"部门最多 {MAX_DEPTH} 级")
        await self._check_sibling(parent.uuid, name)
        sort = await self.session.scalar(
            select(func.coalesce(func.max(Dept.sort), 0) + 1).where(Dept.parent_uuid == parent.uuid)
        )
        uuid = uuid7()
        dept = Dept(
            uuid=uuid,
            parent_uuid=parent.uuid,
            name=name,
            sort=sort,
            path=f"{parent.path}{uuid}/",
            depth=parent.depth + 1,
            created_by=self.actor.user_uuid,
            updated_by=self.actor.user_uuid,
        )
        self.session.add(dept)
        await self.session.flush()
        record(self.session, self.actor, "dept.create", "dept", dept.uuid, name, parent=parent.name)
        return dept

    async def create_root(self, name: str) -> Dept:
        if await self.root() is not None:
            raise Conflict("ROOT_EXISTS", "根部门已存在")
        uuid = uuid7()
        dept = Dept(uuid=uuid, parent_uuid=None, name=_check_name(name), path=f"/{uuid}/", depth=1)
        self.session.add(dept)
        await self.session.flush()
        record(self.session, self.actor, "dept.create", "dept", dept.uuid, dept.name, root=True)
        return dept

    async def _check_sibling(
        self, parent_uuid: UUID | None, name: str, exclude: UUID | None = None
    ) -> None:
        stmt = select(Dept.uuid).where(Dept.parent_uuid == parent_uuid, Dept.name == name)
        if exclude:
            stmt = stmt.where(Dept.uuid != exclude)
        if await self.session.scalar(stmt) is not None:
            raise Conflict("DEPT_NAME_EXISTS", "同一上级下已有同名部门")

    async def update(
        self,
        uuid: UUID,
        *,
        name: str | None = None,
        leader_uuid: Any = UNSET,
        version: int | None = None,
    ) -> Dept:
        dept = await self.get(uuid)
        if version is not None and version != dept.version:
            raise Conflict("VERSION_CONFLICT", "该部门已被他人修改，请刷新后重试")
        changes: dict[str, Any] = {}
        if name is not None:
            new = _check_name(name)
            if new != dept.name:
                await self._check_sibling(dept.parent_uuid, new, exclude=dept.uuid)
                changes["name"] = [dept.name, new]
                dept.name = new
        if leader_uuid is not UNSET and leader_uuid != dept.leader_uuid:
            old = await self._user(dept.leader_uuid)
            if leader_uuid is not None:
                await self._check_leader(dept, leader_uuid)
            new_leader = await self._user(leader_uuid)
            changes["leader"] = [old.name if old else None, new_leader.name if new_leader else None]
            dept.leader_uuid = leader_uuid
        if changes:
            dept.version += 1
            dept.updated_by = self.actor.user_uuid
            action = "dept.leader_set" if list(changes) == ["leader"] else "dept.update"
            record(self.session, self.actor, action, "dept", dept.uuid, dept.name, changes=changes)
        await self.session.flush()
        return dept

    async def move(self, uuid: UUID, parent_uuid: UUID, *, dry_run: bool = False) -> dict[str, Any]:
        from ..perm.effective import super_guard

        await advisory_lock(self.session, "uc_dept_tree")
        dept = await self.get(uuid)
        if dept.parent_uuid is None:
            raise Conflict("ROOT_IMMUTABLE", "根部门不能移动")
        target = await self.get(parent_uuid)
        if target.path.startswith(dept.path):
            raise Invalid("MOVE_INTO_SUBTREE", "不能移动到自己或自己的下级部门下")
        height = (
            await self.session.scalar(
                select(func.max(Dept.depth)).where(Dept.path.like(dept.path + "%"))
            )
            - dept.depth
        )
        if target.depth + 1 + height > MAX_DEPTH:
            raise Invalid(
                "DEPTH_EXCEEDED", f"移动后超过 {MAX_DEPTH} 级（被移动部门有 {height + 1} 层）"
            )
        await self._check_sibling(target.uuid, dept.name, exclude=dept.uuid)

        nested = await self.session.begin_nested()
        old_path, new_path = dept.path, f"{target.path}{dept.uuid}/"
        delta = target.depth + 1 - dept.depth
        old_names = await self.path_names(dept)
        dept_count = len(await self.subtree_uuids(dept))
        member_count = await self.session.scalar(
            select(func.count())
            .select_from(User)
            .join(Dept, Dept.uuid == User.dept_uuid)
            .where(Dept.path.like(old_path + "%"))
        )
        async with super_guard(self.session):
            await self.session.execute(
                update(Dept)
                .where(Dept.path.like(old_path + "%"))
                .values(
                    path=func.concat(new_path, func.substr(Dept.path, len(old_path) + 1)),
                    depth=Dept.depth + delta,
                )
                .execution_options(synchronize_session=False)
            )
            dept.parent_uuid = target.uuid
            dept.version += 1
            await self.session.flush()
            await self.session.refresh(dept)
            cleared = await self.fix_leaders()
        new_names = await self.path_names(dept)
        result = {
            "dept_count": dept_count,
            "member_count": member_count,
            "leaders_to_clear": cleared,
            "path_after": " / ".join(new_names),
        }
        if dry_run:
            await nested.rollback()
            await self.session.refresh(dept)
            return result
        await nested.commit()
        record(
            self.session,
            self.actor,
            "dept.move",
            "dept",
            dept.uuid,
            dept.name,
            path_from=" / ".join(old_names),
            path_to=result["path_after"],
            dept_count=dept_count,
            member_count=member_count,
        )
        return result

    async def delete(self, uuid: UUID, *, dry_run: bool = False) -> dict[str, Any]:
        await advisory_lock(self.session, "uc_dept_tree")
        dept = await self.get(uuid)
        if dept.parent_uuid is None:
            raise Conflict("ROOT_IMMUTABLE", "根部门不能删除")
        children = await self.session.scalar(
            select(func.count()).select_from(Dept).where(Dept.parent_uuid == dept.uuid)
        )
        members = await self.session.scalar(
            select(func.count()).select_from(User).where(User.dept_uuid == dept.uuid)
        )
        if children or members:
            raise Conflict(
                "DEPT_NOT_EMPTY",
                f"部门下还有 {children} 个子部门、{members} 名成员，请先移走或删除",
                children=children,
                members=members,
            )
        apps = list(
            await self.session.scalars(
                select(App.name)
                .join(AppScopeDept, AppScopeDept.app_uuid == App.uuid)
                .where(AppScopeDept.dept_uuid == dept.uuid)
            )
        )
        grants = await self.session.scalar(
            select(func.count()).select_from(Grant).where(Grant.dept_uuid == dept.uuid)
        )
        acls = await self.session.scalar(
            select(func.count()).select_from(DataAcl).where(DataAcl.dept_uuid == dept.uuid)
        )
        impact = {"app_scopes": apps, "grants": grants, "data_acls": acls}
        if dry_run:
            return impact
        names = await self.path_names(dept)
        # 应用范围、授予该部门的授权与数据授权由外键 ON DELETE CASCADE 一并清理
        await self.session.delete(dept)
        await self.session.flush()
        record(
            self.session, self.actor, "dept.delete", "dept", dept.uuid, " / ".join(names), **impact
        )
        return impact

    async def move_in(
        self, uuid: UUID, user_uuids: list[UUID], *, dry_run: bool = False
    ) -> dict[str, Any]:
        from ..perm.effective import super_guard

        dept = await self.get(uuid)
        users = list(await self.session.scalars(select(User).where(User.uuid.in_(user_uuids))))
        if len(users) != len(set(user_uuids)):
            raise Invalid("SUBJECT_NOT_FOUND", "有用户不存在")
        nested = await self.session.begin_nested()
        async with super_guard(self.session):
            for u in users:
                if u.dept_uuid != dept.uuid:
                    old = await self.get(u.dept_uuid)
                    u.dept_uuid = dept.uuid
                    u.version += 1
                    record(
                        self.session,
                        self.actor,
                        "user.dept_change",
                        "user",
                        u.uuid,
                        f"{u.name}（{u.account}）",
                        dept_from=old.name,
                        dept_to=dept.name,
                    )
            await self.session.flush()
            cleared = await self.fix_leaders()
        if dry_run:
            await nested.rollback()
        else:
            await nested.commit()
        return {"moved": len(users), "leaders_to_clear": cleared}


def _user_brief(u: User | None) -> dict[str, Any] | None:
    if u is None:
        return None
    return {"id": str(u.uuid), "name": u.name, "account": u.account, "status": u.status}
