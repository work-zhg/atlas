"""运营账号与登录（c4-model/opscenter/code-accounts.html）。

运营账号不在用户中心的任何租户里；运营人员不出现在任何租户的人员列表里，也不占名额。

    角色     只有两个：ADMIN（运营管理员，全部运营操作）、OBSERVER（运营观察员，只读）
    登录     账号 + 密码（用户 11-20 定：不做双因素）。临时密码登录后必须先改密码
    会话     只存令牌哈希；空闲 session_idle_minutes 或登录满 session_max_hours 失效；
             停用、重置密码、改密码时作废这个人的全部会话
    锁定     连续失败 lock_after_failures 次锁定 lock_minutes 分钟
    保护     没有自助注册；第一个账号部署时用命令建；不能停用或降级最后一个可用的运营管理员
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import Who, record
from .db.models import Operator, OpsSession
from .db.types import utcnow
from .errors import Conflict, Forbidden, Invalid, NotFound, Unauthorized
from .security import (
    check_policy,
    hash_password,
    hash_token,
    new_token,
    temp_password,
    verify_password,
)
from .settings import OpsSettings

__all__ = ["ROLES", "Accounts", "Principal"]

ROLES = ("ADMIN", "OBSERVER")
_ACCOUNT_RE = re.compile(r"^[a-z0-9._]{3,32}$")
#: 会话最后活动时间最多隔这么久写一次，免得每个请求都写库
_TOUCH_EVERY = timedelta(minutes=1)


@dataclass(frozen=True)
class Principal:
    """一个请求的运营人员。随调用写进 OperatorStamp 带给业务系统。"""

    operator_uuid: UUID
    account: str
    name: str
    role: str
    must_change_password: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    @property
    def who(self) -> Who:
        return Who(self.operator_uuid, self.name)


def _principal(op: Operator) -> Principal:
    return Principal(op.operator_uuid, op.account, op.name, op.role, op.must_change_password)


class Accounts:
    def __init__(self, session: AsyncSession, settings: OpsSettings) -> None:
        self.session = session
        self.settings = settings

    # ───────────────────────────── 登录

    async def login(self, account: str, password: str, ip: str | None) -> tuple[str, Operator]:
        """→ (会话令牌, 运营账号)。账号不存在、密码错、停用一律同一句提示，不让人枚举账号。"""
        now = utcnow()
        op = await self._by_account(account)
        if op is None or op.status != "active":
            record(self.session, Who(None, None), "login.failed", target=account[:32])
            await self.session.commit()  # 这次请求以错误结束，审计也要留下
            raise Unauthorized("LOGIN_FAILED", "账号或密码错误")
        if op.locked_until is not None and op.locked_until > now:
            raise Unauthorized("ACCOUNT_LOCKED", "密码错误次数太多，账号已暂时锁定，请稍后再试")
        if not verify_password(op.password_hash, password):
            op.failed_logins += 1
            action = "login.failed"
            if op.failed_logins >= self.settings.lock_after_failures:
                op.locked_until = now + timedelta(minutes=self.settings.lock_minutes)
                op.failed_logins = 0
                action = "login.locked"
            record(self.session, Who(op.operator_uuid, op.name), action, target=op.account)
            # 计数要留下来：这次请求以错误结束，但失败次数必须提交
            await self.session.commit()
            raise Unauthorized("LOGIN_FAILED", "账号或密码错误")
        op.failed_logins = 0
        op.locked_until = None
        op.last_login_at = now
        token = new_token()
        self.session.add(
            OpsSession(
                operator_uuid=op.operator_uuid,
                token_hash=hash_token(token),
                ip=ip,
                last_seen_at=now,
                expires_at=now + timedelta(hours=self.settings.session_max_hours),
            )
        )
        record(self.session, Who(op.operator_uuid, op.name), "login.ok", target=op.account)
        await self.session.flush()
        return token, op

    async def resolve(self, token: str | None) -> Principal:
        if not token:
            raise Unauthorized("NOT_LOGGED_IN", "请先登录")
        now = utcnow()
        sess = await self.session.scalar(
            select(OpsSession).where(OpsSession.token_hash == hash_token(token))
        )
        idle = timedelta(minutes=self.settings.session_idle_minutes)
        if sess is None or sess.expires_at <= now or sess.last_seen_at + idle <= now:
            raise Unauthorized("NOT_LOGGED_IN", "登录已过期，请重新登录")
        op = await self.session.scalar(
            select(Operator).where(Operator.operator_uuid == sess.operator_uuid)
        )
        if op is None or op.status != "active":
            raise Unauthorized("NOT_LOGGED_IN", "账号已停用")
        if now - sess.last_seen_at >= _TOUCH_EVERY:
            sess.last_seen_at = now
        return _principal(op)

    async def logout(self, token: str | None) -> None:
        if token:
            await self.session.execute(
                delete(OpsSession).where(OpsSession.token_hash == hash_token(token))
            )

    async def change_password(self, p: Principal, old: str, new: str, token: str) -> None:
        op = await self._get(p.operator_uuid)
        if not verify_password(op.password_hash, old):
            raise Invalid("WRONG_PASSWORD", "原密码不对", field="old_password")
        self._check_password(new)
        if verify_password(op.password_hash, new):
            raise Invalid("SAME_PASSWORD", "新密码不能和原密码一样", field="new_password")
        op.password_hash = hash_password(new)
        op.must_change_password = False
        op.version += 1
        # 别的地方的登录作废，这一个保留
        await self.session.execute(
            delete(OpsSession).where(
                OpsSession.operator_uuid == op.operator_uuid,
                OpsSession.token_hash != hash_token(token),
            )
        )
        record(self.session, p.who, "operator.change_password", target=op.account)

    # ───────────────────────────── 账号管理（只有运营管理员）

    async def list(self) -> list[Operator]:
        return list(await self.session.scalars(select(Operator).order_by(Operator.id)))

    async def create(self, account: str, name: str, role: str, by: Who) -> tuple[Operator, str]:
        account = (account or "").strip().lower()
        if not _ACCOUNT_RE.match(account):
            raise Invalid(
                "VALIDATION_FAILED", "账号：小写字母、数字、点和下划线，3–32 位", field="account"
            )
        if await self._by_account(account) is not None:
            raise Conflict("ACCOUNT_EXISTS", "账号已存在")
        op = Operator(
            account=account,
            name=_name(name),
            role=_role(role),
            status="active",
            password_hash="",
            must_change_password=True,
        )
        temp = temp_password()
        op.password_hash = hash_password(temp)
        self.session.add(op)
        await self.session.flush()
        record(self.session, by, "operator.create", target=account, detail={"role": op.role})
        return op, temp

    async def bootstrap(self, account: str, name: str) -> str:
        """部署时建第一个运营管理员（那时系统里还没有任何人能登录）。已经有账号就拒绝。"""
        if await self.session.scalar(select(func.count()).select_from(Operator)):
            raise Conflict(
                "ALREADY_BOOTSTRAPPED", "已经有运营账号了，bootstrap 只能在首次部署时执行"
            )
        _op, temp = await self.create(account, name, "ADMIN", Who.system())
        return temp

    async def update(self, operator_uuid: UUID, data: dict[str, str], by: Who) -> Operator:
        op = await self._get(operator_uuid)
        changes: dict[str, list[str]] = {}
        if "name" in data:
            name = _name(data["name"])
            if name != op.name:
                changes["name"] = [op.name, name]
                op.name = name
        if "role" in data:
            role = _role(data["role"])
            if role != op.role:
                if op.role == "ADMIN":
                    await self._keep_an_admin(op)
                changes["role"] = [op.role, role]
                op.role = role
        if changes:
            op.version += 1
            record(self.session, by, "operator.update", target=op.account, detail=changes)
        await self.session.flush()
        return op

    async def set_status(self, operator_uuid: UUID, active: bool, by: Who) -> Operator:
        op = await self._get(operator_uuid)
        if by.operator_uuid == op.operator_uuid and not active:
            raise Forbidden("CANNOT_DISABLE_SELF", "不能停用自己")
        status = "active" if active else "disabled"
        if op.status != status:
            if not active and op.role == "ADMIN":
                await self._keep_an_admin(op)
            op.status = status
            op.version += 1
            if not active:
                await self._revoke_sessions(op.operator_uuid)
            record(
                self.session,
                by,
                "operator.enable" if active else "operator.disable",
                target=op.account,
            )
        await self.session.flush()
        return op

    async def reset_password(self, operator_uuid: UUID, by: Who) -> tuple[Operator, str]:
        """给一个新的临时密码（只出现在这次响应里）；他的全部登录作废、锁定解除。"""
        op = await self._get(operator_uuid)
        temp = temp_password()
        op.password_hash = hash_password(temp)
        op.must_change_password = True
        op.failed_logins = 0
        op.locked_until = None
        op.version += 1
        await self._revoke_sessions(op.operator_uuid)
        record(self.session, by, "operator.reset_password", target=op.account)
        await self.session.flush()
        return op, temp

    # ───────────────────────────── 内部

    def _check_password(self, raw: str) -> None:
        errs = check_policy(raw, self.settings.password_min_len)
        if errs:
            raise Invalid(
                "PASSWORD_POLICY",
                "新密码要求：" + "、".join(errs),
                field="new_password",
                unmet=errs,
            )

    async def _keep_an_admin(self, op: Operator) -> None:
        others = await self.session.scalar(
            select(func.count())
            .select_from(Operator)
            .where(
                Operator.role == "ADMIN",
                Operator.status == "active",
                Operator.operator_uuid != op.operator_uuid,
            )
        )
        if not others:
            raise Conflict("LAST_ADMIN", "至少要保留一个可用的运营管理员")

    async def _revoke_sessions(self, operator_uuid: UUID) -> None:
        await self.session.execute(
            delete(OpsSession).where(OpsSession.operator_uuid == operator_uuid)
        )

    async def _by_account(self, account: str) -> Operator | None:
        return await self.session.scalar(
            select(Operator).where(Operator.account == (account or "").strip().lower())
        )

    async def _get(self, operator_uuid: UUID) -> Operator:
        op = await self.session.scalar(
            select(Operator).where(Operator.operator_uuid == operator_uuid)
        )
        if op is None:
            raise NotFound("OPERATOR_NOT_FOUND", "运营账号不存在")
        return op


def _name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name or len(name) > 32:
        raise Invalid("VALIDATION_FAILED", "名字：1–32 个字", field="name")
    return name


def _role(raw: str | None) -> str:
    if raw not in ROLES:
        raise Invalid("VALIDATION_FAILED", "角色只能是 ADMIN 或 OBSERVER", field="role")
    return raw
