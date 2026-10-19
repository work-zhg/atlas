"""用户中心管理台的登录与会话（只服务于用户中心自身，不对外提供 SSO）。

★ 会话首版存 PG（uc_session），令牌只存 SHA-256；Cookie HttpOnly + SameSite=Lax，
  写请求另校验双提交的 CSRF 令牌（api/deps.py）。
★ 租户（租户设计 §08）：管理台登录 = 企业代码 + 账号 + 密码；账号只在租户内唯一。
  只有一个租户时可以不填企业代码。企业代码不存在与账号密码错误给同一句提示，不让人枚举租户。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..db.models import Credential, LoginLog, Session, Tenant, User
from ..db.tenancy import current_tenant, platform_scope, set_tenant
from ..db.types import utcnow
from ..errors import Forbidden, Unauthorized
from ..security import hash_token, new_token, verify_password
from ..settings import UCSettings

__all__ = ["AuthService", "LoginResult", "Verified"]


@dataclass
class LoginResult:
    user: User
    token: str
    session_uuid: UUID
    must_change_password: bool


@dataclass(frozen=True)
class Verified:
    """网关校验的结果（内网身份传递 §4）：写进 X-Uid / X-Tenant-Id。"""

    user_uuid: UUID
    tenant_uuid: UUID
    must_change_password: bool


class _VerifyCache:
    """令牌摘要 → (过期时刻, 结果)。网关每个请求都来校验一次：进程内短缓存，只缓存成功结果。

    ★ 多副本时登出 / 停用最多滞后一个 TTL（秒级）；本进程登出立即失效。
    """

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, Verified]] = {}

    def get(self, key: str) -> Verified | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: str, v: Verified, ttl: float) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, v)

    def drop(self, key: str) -> None:
        self._rows.pop(key, None)


verify_cache = _VerifyCache()


class AuthService:
    def __init__(self, session: AsyncSession, settings: UCSettings) -> None:
        self.session = session
        self.settings = settings
        #: 登录日志里的应用名（历史上开放接口代为校验密码时写；现在只有管理台登录，恒为空）
        self._app_name: str | None = None

    def _log(
        self, account: str, user: User | None, ip: str | None, ok: bool, reason: str | None = None
    ) -> None:
        self.session.add(
            LoginLog(
                tenant_uuid=current_tenant(self.session),
                user_uuid=user.uuid if user else None,
                account=account[:64],
                ip=ip,
                ok=ok,
                reason=reason,
                app_name=self._app_name,
            )
        )

    async def enter_tenant(self, code: str | None, account: str, ip: str | None) -> Tenant:
        """按企业代码进入租户（建立租户上下文）。只有一个租户时企业代码可省。"""
        from ..tenant.service import find_by_code, sole_tenant, tenant_usable

        with platform_scope(self.session):
            tenant = (
                await find_by_code(self.session, code) if code else await sole_tenant(self.session)
            )
        if tenant is None:
            self._log(
                account.strip().lower(), None, ip, False, "企业代码不存在" if code else "缺企业代码"
            )
            if not code:
                raise Unauthorized("TENANT_REQUIRED", "请填写企业代码")
            raise Unauthorized("LOGIN_FAILED", "企业代码、账号或密码错误")
        set_tenant(self.session, tenant.uuid)
        if not tenant_usable(tenant):
            self._log(account.strip().lower(), None, ip, False, "租户已停用")
            raise Forbidden("TENANT_DISABLED", "企业账号已停用或已到期，请联系管理员")
        return tenant

    async def login(
        self, account: str, password: str, ip: str | None, tenant_code: str | None = None
    ) -> LoginResult:
        await self.enter_tenant(tenant_code, account, ip)
        user, cred, account = await self._authenticate(account, password, ip)
        now = utcnow()
        user.last_login_at = now
        user.last_login_ip = ip
        token = new_token()
        sess = Session(
            token_hash=hash_token(token),
            user_uuid=user.uuid,
            ip=ip,
            expires_at=now + timedelta(hours=self.settings.session_hours),
        )
        self.session.add(sess)
        self._log(account, user, ip, True)
        await self.session.flush()
        return LoginResult(user, token, sess.uuid, cred.must_change_password)

    async def _authenticate(
        self, account: str, password: str, ip: str | None
    ) -> tuple[User, Credential, str]:
        account = (account or "").strip().lower()
        user = (
            await self.session.execute(select(User).where(User.account == account))
        ).scalar_one_or_none()
        cred = (
            (
                await self.session.execute(
                    select(Credential).where(Credential.user_uuid == user.uuid)
                )
            ).scalar_one_or_none()
            if user
            else None
        )
        now = utcnow()
        if user is None or cred is None:
            self._log(account, None, ip, False, "账号不存在")
            raise Unauthorized("LOGIN_FAILED", "企业代码、账号或密码错误")
        if user.status == "disabled":
            self._log(account, user, ip, False, "账号已停用")
            raise Forbidden("USER_DISABLED", "账号已停用，请联系管理员")
        if cred.locked_until is not None:
            if cred.locked_until > now:
                self._log(account, user, ip, False, "账号已锁定")
                minutes = max(1, int((cred.locked_until - now).total_seconds() // 60) + 1)
                raise Forbidden(
                    "ACCOUNT_LOCKED", f"账号已锁定，请 {minutes} 分钟后重试或联系管理员解锁"
                )
            cred.locked_until = None
            cred.failed_count = 0
        if not verify_password(cred.password_hash, password or ""):
            cred.failed_count += 1
            left = self.settings.lock_after_failures - cred.failed_count
            self._log(account, user, ip, False, "密码错误")
            if left <= 0:
                cred.locked_until = now + timedelta(minutes=self.settings.lock_minutes)
                cred.failed_count = 0
                record(
                    self.session,
                    Actor.system(),
                    "user.lock",
                    "user",
                    user.uuid,
                    f"{user.name}（{user.account}）",
                    reason=f"连续 {self.settings.lock_after_failures} 次密码错误",
                )
                await self.session.flush()
                raise Forbidden(
                    "ACCOUNT_LOCKED",
                    f"连续 {self.settings.lock_after_failures} 次密码错误，账号已锁定",
                )
            await self.session.flush()
            raise Unauthorized(
                "LOGIN_FAILED", f"企业代码、账号或密码错误（再错 {left} 次将锁定）", left=left
            )
        if (
            cred.must_change_password
            and cred.temp_expires_at is not None
            and cred.temp_expires_at < now
        ):
            self._log(account, user, ip, False, "临时密码已过期")
            raise Forbidden("TEMP_PASSWORD_EXPIRED", "临时密码已过期，请联系管理员重置")
        if (
            self.settings.password_expire_days > 0
            and cred.password_changed_at is not None
            and cred.password_changed_at + timedelta(days=self.settings.password_expire_days) < now
        ):
            cred.must_change_password = True
        cred.failed_count = 0
        return user, cred, account

    async def resolve(self, token: str | None) -> tuple[User, Session, Credential]:
        if not token:
            raise Unauthorized("NOT_LOGGED_IN", "请先登录")
        sess = (
            await self.session.execute(
                select(Session).where(Session.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if sess is None or sess.expires_at < utcnow():
            raise Unauthorized("NOT_LOGGED_IN", "登录已过期，请重新登录")
        user = (
            await self.session.execute(select(User).where(User.uuid == sess.user_uuid))
        ).scalar_one()
        if user.status == "disabled":
            raise Unauthorized("NOT_LOGGED_IN", "账号已停用")
        cred = (
            await self.session.execute(select(Credential).where(Credential.user_uuid == user.uuid))
        ).scalar_one()
        return user, sess, cred

    async def verify(self, token: str | None) -> Verified:
        """网关 auth_request（内网身份传递 §4）：会话 → (用户, 租户)。失败抛 Unauthorized。"""
        from ..tenant.service import get_tenant, tenant_usable

        key = hash_token(token) if token else ""
        hit = verify_cache.get(key) if key else None
        if hit is not None:
            return hit
        with platform_scope(self.session):
            user, _, cred = await self.resolve(token)
            tenant = await get_tenant(self.session, user.tenant_uuid)
        if not tenant_usable(tenant):
            raise Unauthorized("TENANT_DISABLED", "企业账号已停用或已到期，请联系管理员")
        v = Verified(user.uuid, tenant.uuid, cred.must_change_password)
        verify_cache.put(key, v, self.settings.verify_cache_ttl_s)
        return v

    async def logout(self, token: str | None) -> None:
        if token:
            verify_cache.drop(hash_token(token))
            await self.session.execute(
                delete(Session).where(Session.token_hash == hash_token(token))
            )
