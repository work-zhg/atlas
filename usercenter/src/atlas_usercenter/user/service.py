"""用户管理（用户设计）。

账号不可改、用户不物理删除；临时密码只在响应里出现一次；停用即注销管理台会话；
岗位与角色不是用户属性（权限管理授予）；直属上级由组织计算。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, mask_email, mask_phone, record
from ..common import ACCOUNT_RE, EMAIL_RE, PHONE_RE, Page, display_status, require_text
from ..db.models import Credential, Dept, LoginLog, Session, User
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from ..security import check_policy, hash_password, temp_password, verify_password
from ..settings import UCSettings

__all__ = ["UserService"]


class UserService:
    def __init__(self, session: AsyncSession, actor: Actor, settings: UCSettings) -> None:
        self.session = session
        self.actor = actor
        self.settings = settings

    # ───────────────────────────── 读取

    async def get(self, uuid: UUID) -> User:
        user = (
            await self.session.execute(select(User).where(User.uuid == uuid))
        ).scalar_one_or_none()
        if user is None:
            raise NotFound("USER_NOT_FOUND", "用户不存在")
        return user

    async def credential(self, user_uuid: UUID) -> Credential | None:
        return (
            await self.session.execute(select(Credential).where(Credential.user_uuid == user_uuid))
        ).scalar_one_or_none()

    def _status_expr(self):  # type: ignore[no-untyped-def]
        now = utcnow()
        return case(
            (User.status == "disabled", "disabled"),
            (Credential.locked_until > now, "locked"),
            (User.activated_at.is_(None), "pending"),
            else_="active",
        )

    async def list(
        self, *, status: str | None, dept_uuid: UUID | None, q: str | None, page: Page
    ) -> dict[str, Any]:
        st = self._status_expr().label("display_status")
        base = select(User, Credential, st).outerjoin(Credential, Credential.user_uuid == User.uuid)
        if dept_uuid is not None:
            path = await self.session.scalar(select(Dept.path).where(Dept.uuid == dept_uuid))
            if path is None:
                raise NotFound("DEPT_NOT_FOUND", "部门不存在")
            base = base.join(Dept, Dept.uuid == User.dept_uuid).where(Dept.path.like(path + "%"))
        if q:
            like = f"%{q.strip().lower()}%"
            base = base.where(
                or_(
                    func.lower(User.name).like(like),
                    User.account.like(like),
                    User.email.like(like),
                    func.coalesce(User.phone, "").like(like),
                )
            )
        sub = base.subquery()
        counts = dict(
            (
                await self.session.execute(
                    select(sub.c.display_status, func.count()).group_by(sub.c.display_status)
                )
            ).all()
        )
        counts["all"] = sum(counts.values())
        if status and status != "all":
            base = base.where(st == status)
        total = await self.session.scalar(select(func.count()).select_from(base.subquery()))
        rows = (
            await self.session.execute(
                base.order_by(User.created_at.desc(), User.id.desc())
                .offset(page.offset)
                .limit(page.limit)
            )
        ).all()
        return {"total": total, "counts": counts, "rows": rows}

    # ───────────────────────────── 校验

    def _check_profile(self, data: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if creating or "name" in data:
            out["name"] = require_text(data.get("name"), "name", "姓名", 32)
        if creating:
            account = (data.get("account") or "").strip()
            if not ACCOUNT_RE.match(account):
                raise Invalid(
                    "VALIDATION_FAILED",
                    "账号格式不正确：小写字母、数字、点、下划线，3–32 位",
                    field="account",
                )
            out["account"] = account
        if creating or "email" in data:
            email = (data.get("email") or "").strip().lower()
            if not EMAIL_RE.match(email) or len(email) > 128:
                raise Invalid("VALIDATION_FAILED", "邮箱格式不正确", field="email")
            out["email"] = email
        if "phone" in data:
            phone = (data.get("phone") or "").strip() or None
            if phone and not PHONE_RE.match(phone):
                raise Invalid("VALIDATION_FAILED", "手机格式不正确（11 位手机号）", field="phone")
            out["phone"] = phone
        if creating or "dept_uuid" in data:
            if not data.get("dept_uuid"):
                raise Invalid("VALIDATION_FAILED", "请选择部门", field="dept_id")
            out["dept_uuid"] = data["dept_uuid"]
        return out

    async def _check_unique(
        self, *, account: str | None = None, email: str | None = None, exclude: UUID | None = None
    ) -> None:
        if account and await self.session.scalar(select(User.uuid).where(User.account == account)):
            raise Conflict("ACCOUNT_EXISTS", "账号已存在")
        if email:
            stmt = select(User.uuid).where(User.email == email)
            if exclude:
                stmt = stmt.where(User.uuid != exclude)
            if await self.session.scalar(stmt):
                raise Conflict("EMAIL_EXISTS", "邮箱已被其他用户使用")

    async def _check_dept(self, dept_uuid: UUID) -> Dept:
        dept = (
            await self.session.execute(select(Dept).where(Dept.uuid == dept_uuid))
        ).scalar_one_or_none()
        if dept is None:
            raise Invalid("DEPT_NOT_FOUND", "部门不存在", field="dept_id")
        return dept

    # ───────────────────────────── 写操作

    async def create(self, data: dict[str, Any]) -> tuple[User, str]:
        clean = self._check_profile(data, creating=True)
        await self._check_unique(account=clean["account"], email=clean["email"])
        dept = await self._check_dept(clean["dept_uuid"])
        user = User(
            **clean,
            status="active",
            created_by=self.actor.user_uuid,
            updated_by=self.actor.user_uuid,
        )
        self.session.add(user)
        await self.session.flush()
        temp = temp_password(self.settings)
        self.session.add(
            Credential(
                user_uuid=user.uuid,
                password_hash=hash_password(temp),
                must_change_password=True,
                temp_expires_at=utcnow() + timedelta(days=self.settings.temp_password_days),
            )
        )
        record(
            self.session,
            self.actor,
            "user.create",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
            dept=dept.name,
        )
        await self.session.flush()
        return user, temp

    async def update(
        self, uuid: UUID, data: dict[str, Any], *, version: int | None = None
    ) -> tuple[User, list[str]]:
        from ..org.service import OrgService
        from ..perm.effective import super_guard

        user = await self.get(uuid)
        if version is not None and version != user.version:
            raise Conflict("VERSION_CONFLICT", "该用户已被他人修改，请刷新后重试")
        clean = self._check_profile(data, creating=False)
        if "email" in clean:
            await self._check_unique(email=clean["email"], exclude=user.uuid)
        changes: dict[str, Any] = {}
        cleared: list[str] = []
        dept_changed = "dept_uuid" in clean and clean["dept_uuid"] != user.dept_uuid
        if dept_changed:
            new_dept = await self._check_dept(clean["dept_uuid"])
            old_dept = await self._check_dept(user.dept_uuid)
            changes["dept"] = [old_dept.name, new_dept.name]
        for key in ("name", "email", "phone"):
            if key in clean and clean[key] != getattr(user, key):
                old, new = getattr(user, key), clean[key]
                if key == "email":
                    old, new = mask_email(old), mask_email(new)
                if key == "phone":
                    old, new = mask_phone(old), mask_phone(new)
                changes[key] = [old, new]
                setattr(user, key, clean[key])
        if dept_changed:
            # 调部门会改变继承的授权：经过超级管理员保护；负责人脱离子树的自动清空
            async with super_guard(self.session):
                user.dept_uuid = clean["dept_uuid"]
                await self.session.flush()
                cleared = await OrgService(self.session, self.actor).fix_leaders()
        if changes:
            user.version += 1
            user.updated_by = self.actor.user_uuid
            record(
                self.session,
                self.actor,
                "user.update",
                "user",
                user.uuid,
                f"{user.name}（{user.account}）",
                changes=changes,
            )
        await self.session.flush()
        return user, cleared

    async def _revoke_sessions(self, user_uuid: UUID, *, keep: UUID | None = None) -> None:
        stmt = delete(Session).where(Session.user_uuid == user_uuid)
        if keep is not None:
            stmt = stmt.where(Session.uuid != keep)
        await self.session.execute(stmt)

    async def disable(self, uuid: UUID, reason: str | None) -> User:
        from ..perm.effective import super_guard

        if self.actor.user_uuid == uuid:
            raise Conflict("CANNOT_DISABLE_SELF", "不能停用自己")
        user = await self.get(uuid)
        if user.status == "disabled":
            return user
        async with super_guard(self.session):
            user.status = "disabled"
            user.disabled_at = utcnow()
            user.disabled_by = self.actor.user_uuid
            user.disabled_reason = (reason or "").strip()[:200] or None
            user.version += 1
        await self._revoke_sessions(user.uuid)
        record(
            self.session,
            self.actor,
            "user.disable",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
            reason=user.disabled_reason,
        )
        return user

    async def enable(self, uuid: UUID) -> User:
        user = await self.get(uuid)
        if user.status != "disabled":
            return user
        user.status = "active"
        user.disabled_at = user.disabled_by = user.disabled_reason = None
        user.version += 1
        cred = await self.credential(user.uuid)
        if cred is not None:
            cred.locked_until = None
            cred.failed_count = 0
        record(
            self.session,
            self.actor,
            "user.enable",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
        )
        await self.session.flush()
        return user

    async def unlock(self, uuid: UUID) -> User:
        user = await self.get(uuid)
        if user.status == "disabled":
            raise Conflict("USER_DISABLED", "已停用的用户请先启用")
        cred = await self.credential(user.uuid)
        if cred is not None:
            cred.locked_until = None
            cred.failed_count = 0
        record(
            self.session,
            self.actor,
            "user.unlock",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
        )
        await self.session.flush()
        return user

    async def reset_password(self, uuid: UUID) -> tuple[User, str, Any]:
        user = await self.get(uuid)
        if user.status == "disabled":
            raise Conflict("USER_DISABLED", "已停用的用户不能重置密码，请先启用")
        temp = temp_password(self.settings)
        cred = await self.credential(user.uuid)
        expires = utcnow() + timedelta(days=self.settings.temp_password_days)
        if cred is None:
            cred = Credential(user_uuid=user.uuid, password_hash="")
            self.session.add(cred)
        cred.password_hash = hash_password(temp)
        cred.must_change_password = True
        cred.temp_expires_at = expires
        cred.failed_count = 0
        cred.locked_until = None
        await self._revoke_sessions(user.uuid)
        record(
            self.session,
            self.actor,
            "user.reset_password",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
        )
        await self.session.flush()
        return user, temp, expires

    # ───────────────────────────── 个人中心

    async def update_self(self, user: User, data: dict[str, Any]) -> User:
        clean = self._check_profile(
            {k: v for k, v in data.items() if k in ("email", "phone")}, creating=False
        )
        if "email" in clean:
            await self._check_unique(email=clean["email"], exclude=user.uuid)
        changes = {}
        for key, val in clean.items():
            if val != getattr(user, key):
                mask = mask_email if key == "email" else mask_phone
                changes[key] = [mask(getattr(user, key)), mask(val)]
                setattr(user, key, val)
        if changes:
            user.version += 1
            record(
                self.session,
                Actor.user(user.uuid),
                "user.update_self",
                "user",
                user.uuid,
                f"{user.name}（{user.account}）",
                changes=changes,
            )
        await self.session.flush()
        return user

    async def change_password(
        self, user: User, old: str, new: str, *, keep_session: UUID | None
    ) -> None:
        cred = await self.credential(user.uuid)
        if cred is None or not verify_password(cred.password_hash, old):
            raise Invalid("OLD_PASSWORD_WRONG", "当前密码错误")
        errs = check_policy(new, self.settings)
        if errs:
            raise Invalid("PASSWORD_POLICY", f"密码不符合要求：{'、'.join(errs)}", unmet=errs)
        if verify_password(cred.password_hash, new):
            raise Invalid("PASSWORD_POLICY", "新密码不能与当前密码相同", unmet=["与当前密码不同"])
        cred.password_hash = hash_password(new)
        cred.must_change_password = False
        cred.temp_expires_at = None
        cred.password_changed_at = utcnow()
        first = user.activated_at is None
        if first:
            user.activated_at = utcnow()
        await self._revoke_sessions(user.uuid, keep=keep_session)
        record(
            self.session,
            Actor.user(user.uuid),
            "user.activate" if first else "user.change_password",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
        )
        await self.session.flush()

    async def login_logs(self, uuid: UUID, page: Page) -> list[LoginLog]:
        since = utcnow() - timedelta(days=90)
        return list(
            await self.session.scalars(
                select(LoginLog)
                .where(LoginLog.user_uuid == uuid, LoginLog.occurred_at >= since)
                .order_by(LoginLog.occurred_at.desc())
                .offset(page.offset)
                .limit(page.limit)
            )
        )

    def status_of(self, user: User, cred: Credential | None) -> str:
        return display_status(user, cred)
