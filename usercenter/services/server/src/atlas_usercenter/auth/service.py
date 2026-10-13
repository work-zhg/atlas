"""用户中心管理台的登录与会话（只服务于用户中心自身，不对外提供 SSO）。

★ 会话首版存 PG（uc_session），令牌只存 SHA-256；Cookie HttpOnly + SameSite=Lax，
  写请求另校验双提交的 CSRF 令牌（api/deps.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..db.models import Credential, LoginLog, Session, User
from ..db.types import utcnow
from ..errors import Forbidden, Unauthorized
from ..security import hash_token, new_token, verify_password
from ..settings import UCSettings

__all__ = ["AuthService", "LoginResult"]


@dataclass
class LoginResult:
    user: User
    token: str
    session_uuid: UUID
    must_change_password: bool


class AuthService:
    def __init__(self, session: AsyncSession, settings: UCSettings) -> None:
        self.session = session
        self.settings = settings
        #: 经开放接口校验密码时为调用方应用名（写入登录日志）
        self._app_name: str | None = None

    def _log(
        self, account: str, user: User | None, ip: str | None, ok: bool, reason: str | None = None
    ) -> None:
        self.session.add(
            LoginLog(
                user_uuid=user.uuid if user else None,
                account=account[:64],
                ip=ip,
                ok=ok,
                reason=reason,
                app_name=self._app_name,
            )
        )

    async def login(self, account: str, password: str, ip: str | None) -> LoginResult:
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

    async def verify_for_app(
        self, account: str, password: str, ip: str | None, app_name: str
    ) -> User:
        """开放接口：接入应用代为校验账号密码（本期没有 SSO，以后由 Keycloak 取代）。

        ★ 与管理台登录走同一套失败计数与锁定规则；不创建用户中心会话。
          须改密（临时密码、密码过期）的账号拒绝：先到用户中心改密。
        """
        self._app_name = app_name[:50]
        user, cred, account = await self._authenticate(account, password, ip)
        if cred.must_change_password:
            self._log(account, user, ip, False, "须先修改密码")
            raise Forbidden(
                "PASSWORD_CHANGE_REQUIRED", "首次登录或密码已过期，请先到用户中心修改密码"
            )
        user.last_login_at = utcnow()
        user.last_login_ip = ip
        self._log(account, user, ip, True)
        await self.session.flush()
        return user

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
            raise Unauthorized("LOGIN_FAILED", "账号或密码错误")
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
            raise Unauthorized("LOGIN_FAILED", f"账号或密码错误（再错 {left} 次将锁定）", left=left)
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

    async def logout(self, token: str | None) -> None:
        if token:
            await self.session.execute(
                delete(Session).where(Session.token_hash == hash_token(token))
            )
