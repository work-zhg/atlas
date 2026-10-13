"""身份与权限（权限设计 §03、§10）。

允许 = 角色的操作码 ∧ 团队级别 ∧ 流程角色（仅节点动作）。
- 操作码 / 菜单：登录时取用户中心授权快照存入会话，最长缓存 snapshot_ttl（5 分钟）后重取。
- 团队级别：调用户中心数据权限 check，进程内缓存 team_level_ttl（30 秒）；本进程改了授权立即失效。
★ 登录本期由用户中心代为校验密码（没有 SSO）；以后换 Keycloak 只改 login()。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import TfSession, TfUser
from .db.types import utcnow
from .errors import Forbidden, Unauthorized
from .settings import TFSettings
from .uc import UCClient

__all__ = ["LEVEL_RANK", "Principal", "TeamLevelCache", "login", "logout", "resolve"]

LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Principal:
    user: TfUser
    session_row: TfSession
    snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def uuid(self) -> UUID:
        return self.user.uuid

    @property
    def permissions(self) -> set[str]:
        return set(self.snapshot.get("permissions") or [])

    def has(self, op: str) -> bool:
        return op in self.permissions

    def require(self, *ops: str) -> None:
        missing = [o for o in ops if not self.has(o)]
        if missing:
            raise Forbidden(
                "PERMISSION_DENIED", f"缺少操作权限：{'、'.join(missing)}", missing=missing
            )


async def _upsert_user(session: AsyncSession, info: dict[str, Any]) -> TfUser:
    uid = UUID(info["id"])
    user = (await session.execute(select(TfUser).where(TfUser.uuid == uid))).scalar_one_or_none()
    if user is None:
        user = TfUser(uuid=uid, account=info["account"], name=info["name"])
        session.add(user)
    user.account, user.name = info["account"], info["name"]
    user.email = info.get("email")
    user.status = info.get("status", "active")
    await session.flush()
    return user


async def login(
    session: AsyncSession, uc: UCClient, settings: TFSettings, account: str, password: str
) -> tuple[TfUser, str]:
    r = await uc.verify_password(account, password)
    if not r.get("can_access"):
        raise Forbidden("NO_ACCESS", "你没有访问 AI TeamFlow 的权限，请联系管理员")
    user = await _upsert_user(session, r["user"])
    user.last_login_at = utcnow()
    token = secrets.token_urlsafe(32)
    snap = {k: r[k] for k in ("can_access", "roles", "permissions", "menus")}
    session.add(
        TfSession(
            token_hash=_hash(token),
            user_uuid=user.uuid,
            snapshot=snap,
            snapshot_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=settings.session_hours),
        )
    )
    await session.flush()
    return user, token


async def resolve(
    session: AsyncSession, uc: UCClient, settings: TFSettings, token: str | None
) -> Principal:
    if not token:
        raise Unauthorized("NOT_LOGGED_IN", "请先登录")
    row = (
        await session.execute(select(TfSession).where(TfSession.token_hash == _hash(token)))
    ).scalar_one_or_none()
    if row is None or row.expires_at < utcnow():
        raise Unauthorized("NOT_LOGGED_IN", "登录已过期，请重新登录")
    user = (await session.execute(select(TfUser).where(TfUser.uuid == row.user_uuid))).scalar_one()
    if (utcnow() - row.snapshot_at).total_seconds() > settings.snapshot_ttl_seconds:
        snap = await uc.authz(user.uuid)
        if snap["user"]["status"] == "disabled" or not snap["can_access"]:
            await session.delete(row)
            await session.commit()
            raise Unauthorized("NOT_LOGGED_IN", "账号已停用或已无权访问 AI TeamFlow")
        row.snapshot = {k: snap[k] for k in ("can_access", "roles", "permissions", "menus")}
        row.snapshot_at = utcnow()
        await session.flush()
    return Principal(user, row, dict(row.snapshot))


async def logout(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(delete(TfSession).where(TfSession.token_hash == _hash(token)))


class TeamLevelCache:
    """团队级别的进程内缓存：(用户, 团队) → (级别, 时间)。本进程写授权后调用 invalidate。"""

    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        self._data: dict[tuple[UUID, UUID], tuple[str, float]] = {}

    async def level(self, uc: UCClient, team_uuid: UUID, user_uuid: UUID) -> str:
        hit = self._data.get((user_uuid, team_uuid))
        if hit and time.monotonic() - hit[1] < self.ttl:
            return hit[0]
        level = await uc.team_level(team_uuid, user_uuid)
        self._data[(user_uuid, team_uuid)] = (level, time.monotonic())
        return level

    def invalidate(self, team_uuid: UUID | None = None) -> None:
        if team_uuid is None:
            self._data.clear()
        else:
            for key in [k for k in self._data if k[1] == team_uuid]:
                self._data.pop(key, None)
