"""权限管理 · 数据授权（权限设计 §10、§11）。

级别：只读(1) < 读写(2) < Owner(3)；多来源取最高。只有对该数据有效级别为 Owner 的人能改授权；
任何修改后至少保留一名未停用的 Owner。应用以自身身份只能「写入首个 Owner」和「清理整条数据」，
其他修改必须带 operator 且其为 Owner —— 应用无法绕过 Owner 规则。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..common import Page
from ..db.models import App, DataAcl, DataObject, DataType, Dept, User
from ..errors import Conflict, Forbidden, Invalid, NotFound
from .effective import authz_snapshot, dept_chain, expand_subjects, subject_filter

__all__ = ["LEVELS", "DataService", "level_name"]

LEVELS = {"READ": 1, "WRITE": 2, "OWNER": 3}
_NAMES = {1: "READ", 2: "WRITE", 3: "OWNER"}
_LABELS = {1: "只读", 2: "读写", 3: "Owner"}


def level_name(level: int | None) -> str:
    return _NAMES.get(level or 0, "NONE")


def _parse_level(value: Any) -> int:
    if isinstance(value, int) and value in _NAMES:
        return value
    level = LEVELS.get(str(value or "").upper())
    if level is None:
        raise Invalid("LEVEL_INVALID", "权限级别只能是 READ / WRITE / OWNER")
    return level


class DataService:
    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    # ───────────────────────────── 计算

    async def _user_filter(self, user: User):  # type: ignore[no-untyped-def]
        chain = await dept_chain(self.session, user.dept_uuid)
        return subject_filter(DataAcl, user.uuid, user.dept_uuid, chain[:-1])

    async def level_of(self, user: User, obj_uuid: UUID) -> tuple[int, list[DataAcl]]:
        if user.status == "disabled":
            return 0, []
        hits = list(
            await self.session.scalars(
                select(DataAcl).where(
                    DataAcl.data_object_uuid == obj_uuid, await self._user_filter(user)
                )
            )
        )
        return max((a.level for a in hits), default=0), hits

    async def _active_owner_exists(self, obj_uuid: UUID) -> bool:
        owners = list(
            await self.session.scalars(
                select(DataAcl).where(DataAcl.data_object_uuid == obj_uuid, DataAcl.level == 3)
            )
        )
        users = await expand_subjects(self.session, owners)
        if not users:
            return False
        return (
            await self.session.scalar(
                select(User.uuid)
                .where(User.uuid.in_(list(users)), User.status == "active")
                .limit(1)
            )
        ) is not None

    async def _lock(self, obj_uuid: UUID) -> DataObject:
        """锁住这条数据再做 Owner 校验，避免两名 Owner 同时互相移除。"""
        obj = (
            await self.session.execute(
                select(DataObject).where(DataObject.uuid == obj_uuid).with_for_update()
            )
        ).scalar_one_or_none()
        if obj is None:
            raise NotFound("DATA_NOT_FOUND", "数据不存在")
        return obj

    async def _guard_owner(self, obj_uuid: UUID) -> None:
        await self.session.flush()
        if not await self._active_owner_exists(obj_uuid):
            raise Conflict("LAST_DATA_OWNER", "至少保留一名可用的 Owner")

    async def is_data_admin(self, user: User, obj_uuid: UUID) -> bool:
        """数据管理员：拥有该数据编码配置的「数据管理员操作码」。

        例：TeamFlow 的 Team 配置 team:manage_all，平台管理员可调整任意团队的管理员。
        """
        if user.status == "disabled":
            return False
        dt = (
            await self.session.execute(
                select(DataType)
                .join(DataObject, DataObject.data_type_uuid == DataType.uuid)
                .where(DataObject.uuid == obj_uuid)
            )
        ).scalar_one_or_none()
        if dt is None or not dt.admin_operation_code:
            return False
        app = (await self.session.execute(select(App).where(App.uuid == dt.app_uuid))).scalar_one()
        snap = await authz_snapshot(self.session, user, app)
        return dt.admin_operation_code in snap["permissions"]

    async def _require_owner(
        self, user: User, obj_uuid: UUID, code: str = "NOT_DATA_OWNER"
    ) -> None:
        """修改授权：该数据的 Owner，或该数据编码的数据管理员。"""
        level, _ = await self.level_of(user, obj_uuid)
        if level >= 3 or await self.is_data_admin(user, obj_uuid):
            return
        raise Forbidden(code, "只有 Owner 可以修改这条数据的授权")

    async def _sources(self, hits: list[DataAcl]) -> list[dict[str, Any]]:
        out = []
        for a in hits:
            if a.subject_type == "user":
                out.append({"type": "USER", "level": level_name(a.level)})
            else:
                name = await self.session.scalar(select(Dept.name).where(Dept.uuid == a.dept_uuid))
                out.append(
                    {
                        "type": "DEPT",
                        "dept": name,
                        "include_sub": a.include_sub,
                        "level": level_name(a.level),
                    }
                )
        return out

    # ───────────────────────────── 授权写入（管理台与开放接口共用）

    async def _subjects(self, subjects: list[tuple[str, UUID]]) -> list[str]:
        names = []
        for st, sid in subjects:
            if st == "user":
                u = (
                    await self.session.execute(select(User).where(User.uuid == sid))
                ).scalar_one_or_none()
                if u is None:
                    raise Invalid("SUBJECT_NOT_FOUND", "用户不存在")
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
        return names

    async def _upsert(
        self,
        obj: DataObject,
        level: int,
        subjects: list[tuple[str, UUID]],
        include_sub: bool,
        *,
        source: str,
        app_uuid: UUID | None = None,
    ) -> tuple[int, int]:
        added = updated = 0
        for st, sid in subjects:
            col = DataAcl.user_uuid if st == "user" else DataAcl.dept_uuid
            ex = (
                await self.session.execute(
                    select(DataAcl).where(DataAcl.data_object_uuid == obj.uuid, col == sid)
                )
            ).scalar_one_or_none()
            if ex is not None:
                ex.level = level
                if st == "dept":
                    ex.include_sub = include_sub
                ex.source = source
                ex.granted_by = self.actor.user_uuid
                ex.granted_app_uuid = app_uuid
                updated += 1
                continue
            self.session.add(
                DataAcl(
                    data_object_uuid=obj.uuid,
                    level=level,
                    subject_type=st,
                    user_uuid=sid if st == "user" else None,
                    dept_uuid=sid if st == "dept" else None,
                    include_sub=include_sub if st == "dept" else False,
                    source=source,
                    granted_by=self.actor.user_uuid,
                    granted_app_uuid=app_uuid,
                )
            )
            added += 1
        return added, updated

    async def _obj_label(self, obj: DataObject) -> str:
        code = await self.session.scalar(
            select(DataType.code).where(DataType.uuid == obj.data_type_uuid)
        )
        return f"{code} · {obj.data_name or obj.data_id}"

    # ───────────────────────────── 管理台

    async def list(
        self,
        me: User,
        *,
        scope: str,
        data_code: str | None,
        q: str | None,
        page: Page,
        can_view_all: bool,
    ) -> dict[str, Any]:
        # ★ 管理台会话只属于未停用用户（auth.resolve 已拒绝停用账号），这里无需再判停用
        lvl = (
            select(DataAcl.data_object_uuid.label("obj"), func.max(DataAcl.level).label("level"))
            .where(await self._user_filter(me))
            .group_by(DataAcl.data_object_uuid)
            .subquery()
        )
        stmt = (
            select(DataObject, DataType, lvl.c.level)
            .join(DataType, DataType.uuid == DataObject.data_type_uuid)
            .outerjoin(lvl, lvl.c.obj == DataObject.uuid)
        )
        if scope == "mine":
            stmt = stmt.where(lvl.c.level == 3)
        elif scope == "access":
            stmt = stmt.where(lvl.c.level.in_([1, 2]))
        elif scope == "all":
            if not can_view_all:
                raise Forbidden("PERMISSION_DENIED", "缺少操作权限：data:view")
        else:
            raise Invalid("VALIDATION_FAILED", "scope 只能是 mine / access / all")
        if data_code:
            stmt = stmt.where(DataType.code == data_code)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(DataObject.data_name.like(like), DataObject.data_id.like(like)))
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await self.session.execute(
                stmt.order_by(DataObject.created_at.desc()).offset(page.offset).limit(page.limit)
            )
        ).all()
        apps = {a.uuid: a.name for a in await self.session.scalars(select(App))}
        items = []
        for obj, dt, level in rows:
            acl = list(
                await self.session.scalars(
                    select(DataAcl).where(DataAcl.data_object_uuid == obj.uuid)
                )
            )
            owners = await self._owner_names(acl)
            items.append(
                {
                    "id": str(obj.uuid),
                    "data_code": dt.code,
                    "data_type_name": dt.name,
                    "app": apps.get(dt.app_uuid),
                    "data_id": obj.data_id,
                    "data_name": obj.data_name,
                    "my_level": level_name(level),
                    "owners": owners,
                    "user_grants": sum(1 for a in acl if a.subject_type == "user"),
                    "dept_grants": sum(1 for a in acl if a.subject_type == "dept"),
                    "created_at": obj.created_at.isoformat(),
                }
            )
        counts = {
            "mine": int(
                await self.session.scalar(
                    select(func.count()).select_from(lvl).where(lvl.c.level == 3)
                )
                or 0
            ),
            "access": int(
                await self.session.scalar(
                    select(func.count()).select_from(lvl).where(lvl.c.level.in_([1, 2]))
                )
                or 0
            ),
        }
        if can_view_all:
            counts["all"] = int(
                await self.session.scalar(select(func.count()).select_from(DataObject)) or 0
            )
        return {"total": total, "items": items, "counts": counts}

    async def _owner_names(self, acl: list[DataAcl]) -> list[str]:
        owners = await expand_subjects(self.session, [a for a in acl if a.level == 3])
        if not owners:
            return []
        return list(
            await self.session.scalars(
                select(User.name).where(User.uuid.in_(list(owners)), User.status == "active")
            )
        )

    async def detail(self, me: User, obj_uuid: UUID, *, can_view_all: bool) -> dict[str, Any]:
        obj = (
            await self.session.execute(select(DataObject).where(DataObject.uuid == obj_uuid))
        ).scalar_one_or_none()
        if obj is None:
            raise NotFound("DATA_NOT_FOUND", "数据不存在")
        level, hits = await self.level_of(me, obj.uuid)
        if level == 0 and not can_view_all:
            raise Forbidden("PERMISSION_DENIED", "没有这条数据的权限")
        dt = (
            await self.session.execute(select(DataType).where(DataType.uuid == obj.data_type_uuid))
        ).scalar_one()
        app = (await self.session.execute(select(App).where(App.uuid == dt.app_uuid))).scalar_one()
        acl = list(
            await self.session.scalars(
                select(DataAcl)
                .where(DataAcl.data_object_uuid == obj.uuid)
                .order_by(DataAcl.level.desc(), DataAcl.id)
            )
        )
        users = {
            u.uuid: u
            for u in await self.session.scalars(
                select(User).where(
                    User.uuid.in_(
                        {a.user_uuid for a in acl if a.user_uuid}
                        | {a.granted_by for a in acl if a.granted_by}
                    )
                )
            )
        }
        depts = {d.uuid: d for d in await self.session.scalars(select(Dept))}
        acl_out = []
        for a in acl:
            if a.subject_type == "user":
                u = users[a.user_uuid]  # type: ignore[index]
                subject = {
                    "type": "user",
                    "id": str(u.uuid),
                    "name": u.name,
                    "account": u.account,
                    "status": u.status,
                }
            else:
                d = depts[a.dept_uuid]  # type: ignore[index]
                subject = {"type": "dept", "id": str(d.uuid), "name": d.name}
            acl_out.append(
                {
                    "id": str(a.uuid),
                    "level": level_name(a.level),
                    "subject": subject,
                    "include_sub": a.include_sub,
                    "source": a.source,
                    "granted_by": users[a.granted_by].name
                    if a.granted_by and a.granted_by in users
                    else None,
                    "granted_app": app.name if a.source == "api" else None,
                    "granted_at": a.granted_at.isoformat(),
                }
            )
        holders_map = await expand_subjects(self.session, acl)
        holder_users = (
            {
                u.uuid: u
                for u in await self.session.scalars(
                    select(User).where(User.uuid.in_(list(holders_map)))
                )
            }
            if holders_map
            else {}
        )
        holders = []
        for uid, rows in holders_map.items():
            u = holder_users[uid]
            if u.status == "disabled":
                continue
            holders.append(
                {
                    "user": {"id": str(u.uuid), "name": u.name, "account": u.account},
                    "dept": depts[u.dept_uuid].name,
                    "level": level_name(max(r.level for r in rows)),
                    "sources": await self._sources(rows),
                }
            )
        holders.sort(key=lambda h: (-LEVELS[h["level"]], h["user"]["account"]))
        return {
            "id": str(obj.uuid),
            "data_code": dt.code,
            "data_type_name": dt.name,
            "app": app.name,
            "data_id": obj.data_id,
            "data_name": obj.data_name,
            "created_at": obj.created_at.isoformat(),
            "my_level": level_name(level),
            "my_sources": await self._sources(hits),
            "is_owner": level == 3,
            "can_manage": level == 3 or await self.is_data_admin(me, obj.uuid),
            "acl": acl_out,
            "holders": holders,
            "has_active_owner": await self._active_owner_exists(obj.uuid),
        }

    async def add_acl(
        self,
        me: User,
        obj_uuid: UUID,
        level: Any,
        subjects: list[tuple[str, UUID]],
        include_sub: bool,
    ) -> dict[str, int]:
        obj = await self._lock(obj_uuid)
        await self._require_owner(me, obj.uuid)
        lv = _parse_level(level)
        names = await self._subjects(subjects)
        added, updated = await self._upsert(obj, lv, subjects, include_sub, source="manual")
        await self._guard_owner(obj.uuid)
        record(
            self.session,
            self.actor,
            "data_acl.add",
            "data",
            obj.uuid,
            await self._obj_label(obj),
            level=_LABELS[lv],
            subjects=names,
        )
        return {"added": added, "updated": updated}

    async def _acl(self, acl_uuid: UUID) -> DataAcl:
        a = (
            await self.session.execute(select(DataAcl).where(DataAcl.uuid == acl_uuid))
        ).scalar_one_or_none()
        if a is None:
            raise NotFound("DATA_ACL_NOT_FOUND", "数据授权不存在")
        return a

    async def update_acl(
        self, me: User, acl_uuid: UUID, *, level: Any = None, include_sub: bool | None = None
    ) -> None:
        a = await self._acl(acl_uuid)
        obj = await self._lock(a.data_object_uuid)
        await self._require_owner(me, obj.uuid)
        changes: dict[str, Any] = {}
        if level is not None:
            lv = _parse_level(level)
            if lv != a.level:
                changes["level"] = [_LABELS[a.level], _LABELS[lv]]
                a.level = lv
        if include_sub is not None and a.subject_type == "dept" and include_sub != a.include_sub:
            changes["include_sub"] = include_sub
            a.include_sub = include_sub
        a.source = "manual"
        a.granted_by = me.uuid
        await self._guard_owner(obj.uuid)
        if changes:
            record(
                self.session,
                self.actor,
                "data_acl.update",
                "data",
                obj.uuid,
                await self._obj_label(obj),
                changes=changes,
            )

    async def delete_acl(self, me: User, acl_uuid: UUID) -> None:
        a = await self._acl(acl_uuid)
        obj = await self._lock(a.data_object_uuid)
        await self._require_owner(me, obj.uuid)
        await self.session.delete(a)
        await self._guard_owner(obj.uuid)
        record(
            self.session,
            self.actor,
            "data_acl.delete",
            "data",
            obj.uuid,
            await self._obj_label(obj),
        )

    # ───────────────────────────── 开放接口

    async def _type_of_app(self, app: App, data_code: str) -> DataType:
        dt = (
            await self.session.execute(select(DataType).where(DataType.code == data_code))
        ).scalar_one_or_none()
        if dt is None:
            raise NotFound("DATA_TYPE_NOT_FOUND", "数据编码不存在")
        if dt.app_uuid != app.uuid:
            raise Forbidden("DATA_TYPE_NOT_OWNED", "数据编码不属于调用方应用")
        return dt

    async def _object(self, dt: DataType, data_id: str) -> DataObject | None:
        return (
            await self.session.execute(
                select(DataObject).where(
                    DataObject.data_type_uuid == dt.uuid, DataObject.data_id == data_id
                )
            )
        ).scalar_one_or_none()

    async def open_write(
        self,
        app: App,
        *,
        data_code: str,
        data_id: str,
        data_name: str | None,
        permission: Any,
        subject: tuple[str, UUID],
        include_sub: bool,
        operator: User | None,
    ) -> dict[str, Any]:
        return await self.open_write_batch(
            app,
            data_code=data_code,
            data_id=data_id,
            data_name=data_name,
            grants=[(permission, subject, include_sub)],
            operator=operator,
        )

    async def open_write_batch(
        self,
        app: App,
        *,
        data_code: str,
        data_id: str,
        data_name: str | None,
        grants: list[tuple[Any, tuple[str, UUID], bool]],
        operator: User | None,
    ) -> dict[str, Any]:
        """写入一条或多条授权。

        ★ 数据还没有任何授权时（创建场景），应用可以不带 operator 一次写入多条，
          但其中至少一条是 OWNER；
          已有授权时必须带 operator，且其为 Owner 或数据管理员 —— 应用无法绕过 Owner 规则。
        """
        if not grants:
            raise Invalid("VALIDATION_FAILED", "至少给出一条授权")
        dt = await self._type_of_app(app, data_code)
        data_id = (data_id or "").strip()
        if not data_id or len(data_id) > 64:
            raise Invalid("VALIDATION_FAILED", "data_id 必填，最长 64 字符")
        parsed = [(_parse_level(p), subj, sub) for p, subj, sub in grants]
        await self._subjects([subj for _, subj, _ in parsed])
        obj = await self._object(dt, data_id)
        created = False
        if obj is None:
            obj = DataObject(
                data_type_uuid=dt.uuid,
                data_id=data_id,
                data_name=(data_name or "").strip()[:100] or None,
            )
            self.session.add(obj)
            await self.session.flush()
            created = True
        obj = await self._lock(obj.uuid)
        has_acl = (
            await self.session.scalar(
                select(DataAcl.uuid).where(DataAcl.data_object_uuid == obj.uuid).limit(1)
            )
            is not None
        )
        if has_acl:
            if operator is None:
                raise Forbidden("OWNER_REQUIRED", "数据已有授权，修改须带 operator 且其为 Owner")
            await self._require_owner(operator, obj.uuid, code="OWNER_REQUIRED")
        elif not any(lv == 3 for lv, _, _ in parsed):
            raise Invalid("OWNER_REQUIRED", "数据的第一次授权中必须至少有一条 OWNER")
        if data_name and not obj.data_name:
            obj.data_name = data_name.strip()[:100]
        added = updated = 0
        for lv, subj, sub in parsed:
            a, u = await self._upsert(obj, lv, [subj], sub, source="api", app_uuid=app.uuid)
            added, updated = added + a, updated + u
        await self._guard_owner(obj.uuid)
        record(
            self.session,
            Actor.app(app.uuid),
            "data_acl.api_write",
            "data",
            obj.uuid,
            await self._obj_label(obj),
            grants=[_LABELS[lv] for lv, _, _ in parsed],
            operator=operator.account if operator else None,
        )
        return {
            "data_object_id": str(obj.uuid),
            "data_object_created": created,
            "added": added,
            "updated": updated,
        }

    async def _object_of_app(self, app: App, data_code: str, data_id: str) -> DataObject:
        dt = await self._type_of_app(app, data_code)
        obj = await self._object(dt, data_id)
        if obj is None:
            raise NotFound("DATA_NOT_FOUND", "数据不存在")
        return obj

    async def _acl_view(self, rows: list[DataAcl]) -> list[dict[str, Any]]:
        users = {
            u.uuid: u
            for u in await self.session.scalars(
                select(User).where(User.uuid.in_({a.user_uuid for a in rows if a.user_uuid}))
            )
        }
        depts = {
            d.uuid: d
            for d in await self.session.scalars(
                select(Dept).where(Dept.uuid.in_({a.dept_uuid for a in rows if a.dept_uuid}))
            )
        }
        out = []
        for a in rows:
            if a.subject_type == "user":
                u = users[a.user_uuid]  # type: ignore[index]
                subject = {
                    "type": "USER",
                    "id": str(u.uuid),
                    "account": u.account,
                    "name": u.name,
                    "status": u.status,
                }
            else:
                d = depts[a.dept_uuid]  # type: ignore[index]
                subject = {"type": "DEPT", "id": str(d.uuid), "name": d.name}
            out.append(
                {
                    "id": str(a.uuid),
                    "permission": level_name(a.level),
                    "subject": subject,
                    "include_sub": a.include_sub,
                    "source": a.source,
                    "granted_at": a.granted_at.isoformat(),
                }
            )
        return out

    async def open_acl(self, app: App, *, data_code: str, data_id: str) -> list[dict[str, Any]]:
        obj = await self._object_of_app(app, data_code, data_id)
        rows = list(
            await self.session.scalars(
                select(DataAcl)
                .where(DataAcl.data_object_uuid == obj.uuid)
                .order_by(DataAcl.level.desc(), DataAcl.id)
            )
        )
        return await self._acl_view(rows)

    async def open_holders(
        self, app: App, *, data_code: str, data_id: str, min_level: Any
    ) -> list[dict[str, Any]]:
        """实际拥有者：把部门授权展开为用户，取最高级别；只含未停用用户。"""
        obj = await self._object_of_app(app, data_code, data_id)
        lv = _parse_level(min_level or "READ")
        acl = list(
            await self.session.scalars(select(DataAcl).where(DataAcl.data_object_uuid == obj.uuid))
        )
        mapping = await expand_subjects(self.session, acl)
        if not mapping:
            return []
        users = {
            u.uuid: u
            for u in await self.session.scalars(
                select(User).where(User.uuid.in_(list(mapping)), User.status == "active")
            )
        }
        out = []
        for uid, rows in mapping.items():
            u = users.get(uid)
            level = max(r.level for r in rows)
            if u is None or level < lv:
                continue
            out.append(
                {
                    "user": {"id": str(u.uuid), "account": u.account, "name": u.name},
                    "permission": level_name(level),
                    "sources": await self._sources(rows),
                }
            )
        out.sort(key=lambda h: (-LEVELS[h["permission"]], h["user"]["account"]))
        return out

    async def _acl_of_app(self, app: App, acl_uuid: UUID) -> DataAcl:
        a = await self._acl(acl_uuid)
        dt = (
            await self.session.execute(
                select(DataType)
                .join(DataObject, DataObject.data_type_uuid == DataType.uuid)
                .where(DataObject.uuid == a.data_object_uuid)
            )
        ).scalar_one()
        if dt.app_uuid != app.uuid:
            raise Forbidden("DATA_TYPE_NOT_OWNED", "数据编码不属于调用方应用")
        return a

    async def open_update_acl(
        self,
        app: App,
        acl_uuid: UUID,
        operator: User,
        *,
        level: Any = None,
        include_sub: bool | None = None,
    ) -> None:
        await self._acl_of_app(app, acl_uuid)
        await self.update_acl(operator, acl_uuid, level=level, include_sub=include_sub)

    async def open_delete_acl(self, app: App, acl_uuid: UUID, operator: User) -> None:
        await self._acl_of_app(app, acl_uuid)
        await self.delete_acl(operator, acl_uuid)

    async def open_rename(self, app: App, *, data_code: str, data_id: str, data_name: str) -> None:
        obj = await self._object_of_app(app, data_code, data_id)
        name = (data_name or "").strip()[:100]
        if not name:
            raise Invalid("VALIDATION_FAILED", "data_name 不能为空")
        if name != obj.data_name:
            old, obj.data_name = obj.data_name, name
            record(
                self.session, Actor.app(app.uuid), "data.rename", "data", obj.uuid, name, old=old
            )
            await self.session.flush()

    async def open_check(
        self, app: App, *, data_code: str, data_id: str, user: User
    ) -> dict[str, Any]:
        dt = await self._type_of_app(app, data_code)
        obj = await self._object(dt, data_id)
        if obj is None:
            return {"permission": "NONE", "sources": []}
        level, hits = await self.level_of(user, obj.uuid)
        return {"permission": level_name(level), "sources": await self._sources(hits)}

    async def open_accessible(
        self, app: App, *, data_code: str, user: User, min_level: Any, page: Page
    ) -> dict[str, Any]:
        dt = await self._type_of_app(app, data_code)
        lv = _parse_level(min_level or "READ")
        if user.status == "disabled":
            return {"items": [], "total": 0}
        stmt = (
            select(DataObject.data_id, func.max(DataAcl.level).label("level"))
            .join(DataAcl, DataAcl.data_object_uuid == DataObject.uuid)
            .where(DataObject.data_type_uuid == dt.uuid, await self._user_filter(user))
            .group_by(DataObject.data_id)
            .having(func.max(DataAcl.level) >= lv)
        )
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await self.session.execute(
                stmt.order_by(DataObject.data_id).offset(page.offset).limit(page.limit)
            )
        ).all()
        return {
            "total": total,
            "items": [{"data_id": d, "permission": level_name(level)} for d, level in rows],
        }

    async def open_delete(self, app: App, *, data_code: str, data_id: str) -> dict[str, int]:
        dt = await self._type_of_app(app, data_code)
        obj = await self._object(dt, data_id)
        if obj is None:
            return {"deleted_acls": 0}
        n = int(
            await self.session.scalar(
                select(func.count())
                .select_from(DataAcl)
                .where(DataAcl.data_object_uuid == obj.uuid)
            )
            or 0
        )
        label = await self._obj_label(obj)
        await self.session.delete(obj)
        await self.session.flush()
        record(
            self.session,
            Actor.app(app.uuid),
            "data.cleanup",
            "data",
            obj.uuid,
            label,
            deleted_acls=n,
        )
        return {"deleted_acls": n}
