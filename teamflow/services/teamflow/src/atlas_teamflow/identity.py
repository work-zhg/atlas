"""身份与权限（权限设计 §03、§10；内网身份传递 §3、§10）。

允许 = 角色的操作码 ∧ 团队级别 ∧ 流程角色（仅节点动作）。
- 谁在操作：请求头 X-Uid（网关按用户中心登录态写入）。登录、会话 Cookie、CSRF 都在网关 +
  用户中心，本服务不再处理。
- 操作码 / 菜单：用户中心授权快照，进程内缓存 snapshot_ttl（5 分钟）后重取。
- 团队级别：调用户中心数据权限 check，进程内缓存 team_level_ttl（30 秒）；本进程改了授权立即失效。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import TfUser
from .errors import Forbidden, TFError, Unauthorized
from .settings import TFSettings
from .uc import TEAM_CODE, UCClient

__all__ = ["LEVEL_RANK", "Principal", "TeamLevelCache", "resolve", "snapshots"]

LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


@dataclass
class Principal:
    user: TfUser
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


async def _data_levels(uc: UCClient, user_uuid: UUID) -> dict[str, str]:
    """数据权限领域：对全部团队的级别（数据编码级授权）。与角色快照分开存放，只用于界面提示。

    ★ 取不到按无处理：它只影响「全部团队」的提示文案，真正的判定每次都问用户中心。
    """
    try:
        return {TEAM_CODE: await uc.all_teams_level(user_uuid)}
    except TFError:
        return {}


class _Snapshots:
    """用户 → (取到的时刻, 快照)。进程内；多实例各自缓存，最长滞后一个 TTL。"""

    def __init__(self) -> None:
        self._rows: dict[UUID, tuple[float, dict[str, Any]]] = {}

    def get(self, uid: UUID, ttl: float) -> dict[str, Any] | None:
        row = self._rows.get(uid)
        if row is None or time.monotonic() - row[0] > ttl:
            return None
        return row[1]

    def put(self, uid: UUID, snap: dict[str, Any]) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[uid] = (time.monotonic(), snap)

    def clear(self) -> None:
        self._rows.clear()


snapshots = _Snapshots()


async def resolve(
    session: AsyncSession, uc: UCClient, settings: TFSettings, uid: UUID
) -> Principal:
    """请求头里的人 → 主体。快照过期时重取，并按用户中心资料刷新本地投影（名字、状态）。"""
    snap = snapshots.get(uid, settings.snapshot_ttl_seconds)
    user = (await session.execute(select(TfUser).where(TfUser.uuid == uid))).scalar_one_or_none()
    if snap is None or user is None:
        got = await uc.authz(uid)
        if got["user"]["status"] == "disabled":
            raise Unauthorized("NOT_LOGGED_IN", "账号已停用")
        user = await _upsert_user(session, await uc.user(uid))
        snap = {k: got[k] for k in ("can_access", "roles", "permissions", "menus")}
        snap["data_levels"] = await _data_levels(uc, uid)
        snapshots.put(uid, snap)
    if not snap.get("can_access"):
        raise Forbidden("NO_ACCESS", "你没有访问 AI TeamFlow 的权限，请联系管理员")
    return Principal(user, dict(snap))


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
