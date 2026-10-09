"""权限管理 · 角色授权与岗位（权限设计 §07、§08）。"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..common import Page, require_text
from ..db.models import App, Dept, Grant, Position, PositionRole, Role, User
from ..errors import Conflict, Invalid, NotFound
from .effective import dept_chain, expand_subjects, super_guard

__all__ = ["GrantService", "PositionService"]

POSITION_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")


class GrantService:
    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    async def get(self, uuid: UUID) -> Grant:
        g = (
            await self.session.execute(select(Grant).where(Grant.uuid == uuid))
        ).scalar_one_or_none()
        if g is None:
            raise NotFound("GRANT_NOT_FOUND", "授权不存在")
        return g

    # ───────────────────────────── 展示

    async def describe(self, grants: list[Grant]) -> list[dict[str, Any]]:
        """授权行 → 展示用字典（名称、部门路径、覆盖人数）。"""
        role_ids = {g.role_uuid for g in grants if g.role_uuid}
        pos_ids = {g.position_uuid for g in grants if g.position_uuid}
        user_ids = {g.user_uuid for g in grants if g.user_uuid}
        roles = (
            {
                r.uuid: r
                for r in await self.session.scalars(select(Role).where(Role.uuid.in_(role_ids)))
            }
            if role_ids
            else {}
        )
        apps = {a.uuid: a.name for a in await self.session.scalars(select(App))}
        positions = (
            {
                p.uuid: p
                for p in await self.session.scalars(
                    select(Position).where(Position.uuid.in_(pos_ids))
                )
            }
            if pos_ids
            else {}
        )
        users = (
            {
                u.uuid: u
                for u in await self.session.scalars(select(User).where(User.uuid.in_(user_ids)))
            }
            if user_ids
            else {}
        )
        depts = {d.uuid: d for d in await self.session.scalars(select(Dept))}
        actors = {
            u.uuid: u.name
            for u in await self.session.scalars(
                select(User).where(User.uuid.in_({g.granted_by for g in grants if g.granted_by}))
            )
        }
        out = []
        for g in grants:
            if g.kind == "role":
                r = roles[g.role_uuid]  # type: ignore[index]
                target = {
                    "id": str(r.uuid),
                    "name": r.name,
                    "code": r.code,
                    "app": apps.get(r.app_uuid),
                }
            else:
                p = positions[g.position_uuid]  # type: ignore[index]
                target = {"id": str(p.uuid), "name": p.name, "code": p.code}
            if g.subject_type == "user":
                u = users[g.user_uuid]  # type: ignore[index]
                subject = {
                    "type": "user",
                    "id": str(u.uuid),
                    "name": u.name,
                    "account": u.account,
                    "status": u.status,
                }
                covered = 1
            else:
                d = depts[g.dept_uuid]  # type: ignore[index]
                names = [
                    depts[UUID(x)].name for x in d.path.strip("/").split("/") if UUID(x) in depts
                ]
                subject = {"type": "dept", "id": str(d.uuid), "name": d.name, "path": names}
                q = select(func.count()).select_from(User)
                if g.include_sub:
                    q = q.join(Dept, Dept.uuid == User.dept_uuid).where(
                        Dept.path.like(d.path + "%")
                    )
                else:
                    q = q.where(User.dept_uuid == d.uuid)
                covered = int(await self.session.scalar(q) or 0)
            out.append(
                {
                    "id": str(g.uuid),
                    "kind": g.kind,
                    "target": target,
                    "subject": subject,
                    "include_sub": g.include_sub,
                    "covered": covered,
                    "granted_by": actors.get(g.granted_by) if g.granted_by else None,
                    "granted_at": g.granted_at.isoformat(),
                }
            )
        return out

    async def list(
        self, *, kind: str | None, subject_type: str | None, q: str | None, page: Page
    ) -> dict[str, Any]:
        stmt = select(Grant)
        if kind in ("role", "position"):
            stmt = stmt.where(Grant.kind == kind)
        if subject_type in ("user", "dept"):
            stmt = stmt.where(Grant.subject_type == subject_type)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    Grant.role_uuid.in_(select(Role.uuid).where(Role.name.like(like))),
                    Grant.position_uuid.in_(select(Position.uuid).where(Position.name.like(like))),
                    Grant.user_uuid.in_(
                        select(User.uuid).where(or_(User.name.like(like), User.account.like(like)))
                    ),
                    Grant.dept_uuid.in_(select(Dept.uuid).where(Dept.name.like(like))),
                )
            )
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = list(
            await self.session.scalars(
                stmt.order_by(Grant.granted_at.desc(), Grant.id.desc())
                .offset(page.offset)
                .limit(page.limit)
            )
        )
        return {"total": total, "items": await self.describe(rows)}

    async def subject_grants(self, subject_type: str, uuid: UUID) -> dict[str, Any]:
        """某用户 / 部门的自有授权 + 继承授权（继承的只能在来源处修改）。"""
        if subject_type == "user":
            user = (
                await self.session.execute(select(User).where(User.uuid == uuid))
            ).scalar_one_or_none()
            if user is None:
                raise NotFound("USER_NOT_FOUND", "用户不存在")
            own = list(await self.session.scalars(select(Grant).where(Grant.user_uuid == uuid)))
            chain = await dept_chain(self.session, user.dept_uuid)
            anc = chain[:-1]
            cond = [Grant.dept_uuid == user.dept_uuid]
            if anc:
                cond.append(and_(Grant.include_sub.is_(True), Grant.dept_uuid.in_(anc)))
            inherited = list(await self.session.scalars(select(Grant).where(or_(*cond))))
        elif subject_type == "dept":
            dept = (
                await self.session.execute(select(Dept).where(Dept.uuid == uuid))
            ).scalar_one_or_none()
            if dept is None:
                raise NotFound("DEPT_NOT_FOUND", "部门不存在")
            own = list(await self.session.scalars(select(Grant).where(Grant.dept_uuid == uuid)))
            anc = [UUID(x) for x in dept.path.strip("/").split("/")][:-1]
            inherited = (
                list(
                    await self.session.scalars(
                        select(Grant).where(Grant.include_sub.is_(True), Grant.dept_uuid.in_(anc))
                    )
                )
                if anc
                else []
            )
        else:
            raise Invalid("VALIDATION_FAILED", "授权对象类型只能是 user 或 dept")
        return {"own": await self.describe(own), "inherited": await self.describe(inherited)}

    # ───────────────────────────── 写操作

    async def add(
        self,
        kind: str,
        target_uuids: list[UUID],
        subjects: list[tuple[str, UUID]],
        include_sub: bool,
    ) -> dict[str, int]:
        if kind not in ("role", "position"):
            raise Invalid("VALIDATION_FAILED", "授权内容只能是 role 或 position")
        if not target_uuids or not subjects:
            raise Invalid("VALIDATION_FAILED", "请选择授权内容与授权对象")
        model = Role if kind == "role" else Position
        targets = {
            t.uuid: t
            for t in await self.session.scalars(select(model).where(model.uuid.in_(target_uuids)))
        }
        if len(targets) != len(set(target_uuids)):
            raise Invalid("TARGET_NOT_FOUND", "岗位或角色不存在")
        names: list[str] = []
        for st, sid in subjects:
            if st == "user":
                u = (
                    await self.session.execute(select(User).where(User.uuid == sid))
                ).scalar_one_or_none()
                if u is None:
                    raise Invalid("SUBJECT_NOT_FOUND", "用户不存在")
                if u.status == "disabled":
                    raise Invalid("USER_DISABLED", f"不能授权给已停用的用户：{u.name}")
                names.append(u.name)
            elif st == "dept":
                d = (
                    await self.session.execute(select(Dept).where(Dept.uuid == sid))
                ).scalar_one_or_none()
                if d is None:
                    raise Invalid("SUBJECT_NOT_FOUND", "部门不存在")
                names.append(d.name)
            else:
                raise Invalid("VALIDATION_FAILED", "授权对象类型只能是 user 或 dept")
        added = updated = 0
        col = Grant.role_uuid if kind == "role" else Grant.position_uuid
        async with super_guard(self.session):  # 改「含下级」可能缩小范围
            for tid in targets:
                for st, sid in subjects:
                    scol = Grant.user_uuid if st == "user" else Grant.dept_uuid
                    ex = (
                        await self.session.execute(
                            select(Grant).where(Grant.kind == kind, col == tid, scol == sid)
                        )
                    ).scalar_one_or_none()
                    if ex is not None:
                        if st == "dept" and ex.include_sub != include_sub:
                            ex.include_sub = include_sub
                            updated += 1
                        continue
                    self.session.add(
                        Grant(
                            kind=kind,
                            role_uuid=tid if kind == "role" else None,
                            position_uuid=tid if kind == "position" else None,
                            subject_type=st,
                            user_uuid=sid if st == "user" else None,
                            dept_uuid=sid if st == "dept" else None,
                            include_sub=include_sub if st == "dept" else False,
                            granted_by=self.actor.user_uuid,
                        )
                    )
                    added += 1
        if added or updated:
            record(
                self.session,
                self.actor,
                f"grant.add_{kind}",
                kind,
                None,
                "、".join(t.name for t in targets.values()),
                subjects=names,
                include_sub=include_sub,
                added=added,
                updated=updated,
            )
        return {"added": added, "updated": updated}

    async def set_include_sub(self, uuid: UUID, include_sub: bool) -> Grant:
        g = await self.get(uuid)
        if g.subject_type != "dept":
            raise Invalid("VALIDATION_FAILED", "只有部门授权可以设置「含下级」")
        async with super_guard(self.session):
            g.include_sub = include_sub
        record(
            self.session, self.actor, "grant.scope", "grant", g.uuid, None, include_sub=include_sub
        )
        return g

    async def revoke(self, uuid: UUID, *, dry_run: bool = False) -> dict[str, Any]:
        g = await self.get(uuid)
        info = (await self.describe([g]))[0]
        if dry_run:
            return {"covered": info["covered"]}
        async with super_guard(self.session):
            await self.session.delete(g)
        record(
            self.session,
            self.actor,
            "grant.revoke",
            "grant",
            g.uuid,
            f"{info['target']['name']} → {info['subject']['name']}",
            covered=info["covered"],
        )
        return {"covered": info["covered"]}

    # ───────────────────────────── 角色视角

    async def role_grants(self, role_uuid: UUID) -> dict[str, Any]:
        grants = list(await self.session.scalars(select(Grant).where(Grant.role_uuid == role_uuid)))
        positions = list(
            await self.session.scalars(
                select(Position)
                .join(PositionRole, PositionRole.position_uuid == Position.uuid)
                .where(PositionRole.role_uuid == role_uuid)
            )
        )
        pos_out = []
        for p in positions:
            pg = list(
                await self.session.scalars(select(Grant).where(Grant.position_uuid == p.uuid))
            )
            pos_out.append({"id": str(p.uuid), "name": p.name, "grants": await self.describe(pg)})
        return {"grants": await self.describe(grants), "positions": pos_out}

    async def holders(self, role_uuid: UUID) -> list[dict[str, Any]]:
        from .effective import role_holders

        mapping = await role_holders(self.session, role_uuid)
        if not mapping:
            return []
        users = {
            u.uuid: u
            for u in await self.session.scalars(select(User).where(User.uuid.in_(list(mapping))))
        }
        depts = {d.uuid: d for d in await self.session.scalars(select(Dept))}
        positions = {p.uuid: p.name for p in await self.session.scalars(select(Position))}
        out = []
        for uid, grants in mapping.items():
            u = users[uid]
            sources = []
            for g in grants:
                if g.kind == "position":
                    sources.append(f"岗位：{positions.get(g.position_uuid)}")
                elif g.subject_type == "user":
                    sources.append("直接授权")
                else:
                    sources.append(
                        f"部门：{depts[g.dept_uuid].name}{'（含下级）' if g.include_sub else ''}"
                    )
            out.append(
                {
                    "user": {
                        "id": str(u.uuid),
                        "name": u.name,
                        "account": u.account,
                        "status": u.status,
                    },
                    "dept": depts[u.dept_uuid].name,
                    "sources": sorted(set(sources)),
                }
            )
        out.sort(key=lambda x: x["user"]["account"])
        return out


class PositionService:
    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    async def get(self, uuid: UUID) -> Position:
        p = (
            await self.session.execute(select(Position).where(Position.uuid == uuid))
        ).scalar_one_or_none()
        if p is None:
            raise NotFound("POSITION_NOT_FOUND", "岗位不存在")
        return p

    async def roles_of(self, position_uuid: UUID) -> list[Role]:
        return list(
            await self.session.scalars(
                select(Role)
                .join(PositionRole, PositionRole.role_uuid == Role.uuid)
                .where(PositionRole.position_uuid == position_uuid)
            )
        )

    async def list(self) -> list[dict[str, Any]]:
        positions = list(
            await self.session.scalars(select(Position).order_by(Position.sort, Position.id))
        )
        apps = {a.uuid: a.name for a in await self.session.scalars(select(App))}
        gs = GrantService(self.session, self.actor)
        out = []
        for p in positions:
            roles = await self.roles_of(p.uuid)
            grants = list(
                await self.session.scalars(select(Grant).where(Grant.position_uuid == p.uuid))
            )
            holders = await expand_subjects(self.session, grants)
            out.append(
                {
                    "id": str(p.uuid),
                    "code": p.code,
                    "name": p.name,
                    "version": p.version,
                    "roles": [
                        {
                            "id": str(r.uuid),
                            "name": r.name,
                            "code": r.code,
                            "app": apps.get(r.app_uuid),
                        }
                        for r in roles
                    ],
                    "grants": await gs.describe(grants),
                    "holders": len(holders),
                }
            )
        return out

    async def _check_roles(self, role_uuids: list[UUID]) -> list[Role]:
        roles = (
            list(await self.session.scalars(select(Role).where(Role.uuid.in_(role_uuids))))
            if role_uuids
            else []
        )
        if len(roles) != len(set(role_uuids)):
            raise Invalid("TARGET_NOT_FOUND", "角色不存在")
        return roles

    async def create(self, code: str, name: str, role_uuids: list[UUID]) -> Position:
        code = (code or "").strip()
        if not POSITION_CODE_RE.match(code):
            raise Invalid(
                "VALIDATION_FAILED",
                "岗位编码格式：大写字母开头，大写字母、数字、下划线",
                field="code",
            )
        name = require_text(name, "name", "岗位名称", 32)
        if await self.session.scalar(select(Position.uuid).where(Position.code == code)):
            raise Conflict("POSITION_CODE_EXISTS", "岗位编码已存在")
        if await self.session.scalar(select(Position.uuid).where(Position.name == name)):
            raise Conflict("POSITION_NAME_EXISTS", "岗位名称已存在")
        roles = await self._check_roles(role_uuids)
        sort = await self.session.scalar(select(func.coalesce(func.max(Position.sort), 0) + 1))
        p = Position(
            code=code,
            name=name,
            sort=sort,
            created_by=self.actor.user_uuid,
            updated_by=self.actor.user_uuid,
        )
        self.session.add(p)
        await self.session.flush()
        for r in roles:
            self.session.add(PositionRole(position_uuid=p.uuid, role_uuid=r.uuid))
        record(
            self.session,
            self.actor,
            "position.create",
            "position",
            p.uuid,
            name,
            roles=[r.name for r in roles],
        )
        await self.session.flush()
        return p

    async def update(
        self, uuid: UUID, data: dict[str, Any], version: int | None = None
    ) -> Position:
        p = await self.get(uuid)
        if version is not None and version != p.version:
            raise Conflict("VERSION_CONFLICT", "该岗位已被他人修改，请刷新后重试")
        changes: dict[str, Any] = {}
        if "name" in data:
            name = require_text(data["name"], "name", "岗位名称", 32)
            if name != p.name:
                if await self.session.scalar(
                    select(Position.uuid).where(Position.name == name, Position.uuid != p.uuid)
                ):
                    raise Conflict("POSITION_NAME_EXISTS", "岗位名称已存在")
                changes["name"] = [p.name, name]
                p.name = name
        if "role_uuids" in data:
            roles = await self._check_roles(data["role_uuids"])
            before = {r.name for r in await self.roles_of(p.uuid)}
            # 修改默认角色立即影响获得该岗位的所有人 —— 经过超级管理员保护
            async with super_guard(self.session):
                await self.session.execute(
                    PositionRole.__table__.delete().where(PositionRole.position_uuid == p.uuid)
                )
                for r in roles:
                    self.session.add(PositionRole(position_uuid=p.uuid, role_uuid=r.uuid))
            after = {r.name for r in roles}
            if before != after:
                changes["roles"] = {
                    "added": sorted(after - before),
                    "removed": sorted(before - after),
                }
        if changes:
            p.version += 1
            p.updated_by = self.actor.user_uuid
            record(
                self.session,
                self.actor,
                "position.update",
                "position",
                p.uuid,
                p.name,
                changes=changes,
            )
        await self.session.flush()
        return p

    async def delete(self, uuid: UUID) -> None:
        p = await self.get(uuid)
        n = int(
            await self.session.scalar(
                select(func.count()).select_from(Grant).where(Grant.position_uuid == p.uuid)
            )
            or 0
        )
        if n:
            raise Conflict(
                "POSITION_IN_USE", f"该岗位还有 {n} 条授权，请先在角色授权中撤销", grants=n
            )
        await self.session.delete(p)
        record(self.session, self.actor, "position.delete", "position", p.uuid, p.name)
        await self.session.flush()
