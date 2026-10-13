"""登录会话、授权快照、服务账号（权限设计 §4、§5、§10）。

★ 会话令牌、服务账号密钥都只存 SHA-256；明文只在签发时出现一次。
★ 授权快照（操作码 / 菜单 / 能否访问）按用户进程内缓存 authz_snapshot_ttl_s；刷新时发现
  账号停用或失去 Atlas 访问权 → 删除该用户全部会话，本次请求 401。
★ 角色不存库：来自用户中心快照（存一份就会出现「用户中心已收回、Atlas 还写着有」）。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import AppUser, AuthSession, ServiceAccount
from ..errors import Forbidden, Unauthorized
from .principal import Principal
from .uc import UCClient, UCError

__all__ = [
    "SERVICE_KEY_PREFIX",
    "AuthService",
    "hash_secret",
    "new_service_key",
    "snapshot_cache",
]

log = logging.getLogger("atlas_server.auth")

SERVICE_KEY_PREFIX = "atlas_sk_"
#: 会话续期 / 最近访问时间：最多这么久写一次库，避免每个请求都写
_TOUCH_EVERY = timedelta(minutes=10)


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_service_key() -> tuple[str, str]:
    """→ (明文密钥, 展示用前缀)。"""
    key = SERVICE_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[: len(SERVICE_KEY_PREFIX) + 6]


def _now() -> datetime:
    return datetime.now(UTC)


class _SnapshotCache:
    """用户 id → (过期时刻, 快照)。进程内；多实例各自缓存，最长滞后一个 TTL。"""

    def __init__(self) -> None:
        self._rows: dict[UUID, tuple[float, dict[str, Any]]] = {}

    def get(self, user_id: UUID) -> dict[str, Any] | None:
        row = self._rows.get(user_id)
        if row is None or row[0] < time.monotonic():
            return None
        return row[1]

    def put(self, user_id: UUID, snap: dict[str, Any], ttl: float) -> None:
        self._rows[user_id] = (time.monotonic() + ttl, snap)

    def drop(self, user_id: UUID) -> None:
        self._rows.pop(user_id, None)

    def clear(self) -> None:
        self._rows.clear()


snapshot_cache = _SnapshotCache()


def _user_principal(user: AppUser, snap: dict[str, Any], *, via_cookie: bool) -> Principal:
    return Principal(
        kind="user",
        owner_id=user.id,
        name=user.name,
        operations=frozenset(snap.get("permissions") or []),
        menus=tuple(snap.get("menus") or []),
        via_cookie=via_cookie,
    )


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings, uc: UCClient | None) -> None:
        self.session = session
        self.settings = settings
        self.uc = uc

    def _uc(self) -> UCClient:
        if self.uc is None:
            raise UCError("uc_not_configured", "未接入用户中心", 503)
        return self.uc

    # ───────────────────────────── 用户投影

    async def upsert_user(self, info: dict[str, Any]) -> AppUser:
        """按用户中心资料登记 / 同步投影（id = 用户中心用户 id）。"""
        uid = UUID(str(info["id"]))
        user = await self.session.get(AppUser, uid)
        dept = info.get("dept") or {}
        fields = {
            "name": info.get("name") or info.get("account") or str(uid),
            "external_id": str(uid),
            "account": info.get("account"),
            "email": info.get("email"),
            "dept_id": str(dept["id"]) if dept.get("id") else info.get("dept_id"),
            "dept_name": dept.get("path"),
            "status": "active" if info.get("status", "active") == "active" else "disabled",
            "updated_at": _now(),
        }
        if user is None:
            user = AppUser(id=uid, **fields)
            self.session.add(user)
        else:
            for k, v in fields.items():
                setattr(user, k, v)
        await self.session.flush()
        return user

    # ───────────────────────────── 浏览器会话

    async def login(
        self, account: str, password: str, user_agent: str | None
    ) -> tuple[str, Principal]:
        """用户中心校验密码 → 登记投影 → 建会话。→ (会话令牌明文, 主体)"""
        result = await self._uc().verify_password(account, password)
        if not result.get("can_access"):
            raise Forbidden("没有使用 Atlas 的权限，请联系管理员在用户中心授权")
        user = await self.upsert_user(result["user"])
        if user.status != "active":
            raise Unauthorized("账号已停用")
        snap = {k: result.get(k) for k in ("can_access", "roles", "permissions", "menus")}
        snapshot_cache.put(user.id, snap, self.settings.authz_snapshot_ttl_s)
        token = secrets.token_urlsafe(32)
        self.session.add(
            AuthSession(
                token_hash=hash_secret(token),
                user_id=user.id,
                expires_at=_now() + timedelta(hours=self.settings.session_hours),
                last_used_at=_now(),
                user_agent=(user_agent or "")[:255] or None,
            )
        )
        user.last_seen_at = _now()
        await self.session.flush()
        return token, _user_principal(user, snap, via_cookie=True)

    async def logout(self, token: str | None) -> None:
        if token:
            await self.session.execute(
                delete(AuthSession).where(AuthSession.token_hash == hash_secret(token))
            )

    async def _snapshot(self, user: AppUser) -> dict[str, Any]:
        snap = snapshot_cache.get(user.id)
        if snap is not None:
            return snap
        uc = self._uc()
        snap = await uc.authz(user.id)
        try:
            await self.upsert_user(await uc.user(user.id))  # 资料与状态以用户中心为准
        except UCError as exc:
            if exc.status_code == 404:
                user.status = "disabled"
            else:
                raise
        snapshot_cache.put(user.id, snap, self.settings.authz_snapshot_ttl_s)
        return snap

    async def resolve_session(self, token: str) -> Principal:
        row = await self.session.scalar(
            select(AuthSession).where(AuthSession.token_hash == hash_secret(token))
        )
        now = _now()
        if row is None or row.expires_at <= now:
            raise Unauthorized("登录已失效，请重新登录")
        user = await self.session.get(AppUser, row.user_id)
        if user is None:
            raise Unauthorized("登录已失效，请重新登录")
        snap = await self._snapshot(user)
        if user.status != "active" or not snap.get("can_access"):
            log.info("用户 %s 已停用或失去 Atlas 访问权，清除其会话", user.id)
            await self.revoke_user(user.id)
            raise Unauthorized("登录已失效，请重新登录")
        if row.last_used_at is None or now - row.last_used_at > _TOUCH_EVERY:
            row.last_used_at = now
            row.expires_at = now + timedelta(hours=self.settings.session_hours)
            user.last_seen_at = now
        return _user_principal(user, snap, via_cookie=True)

    async def revoke_user(self, user_id: UUID) -> None:
        snapshot_cache.drop(user_id)
        await self.session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
        await self.session.execute(
            update(AppUser).where(AppUser.id == user_id).values(status="disabled")
        )
        await self.session.commit()

    # ───────────────────────────── 服务账号

    async def resolve_service_key(self, key: str, acting: str | None) -> Principal:
        sa = await self.session.scalar(
            select(ServiceAccount).where(ServiceAccount.key_hash == hash_secret(key))
        )
        if sa is None or sa.status != "active":
            raise Unauthorized("服务账号密钥无效")
        acting_id: UUID | None = None
        if acting and sa.allow_acting:
            try:
                acting_id = UUID(acting)
            except ValueError as exc:
                raise Unauthorized("X-Atlas-Acting-User 必须是用户 id") from exc
        now = _now()
        if sa.last_used_at is None or now - sa.last_used_at > _TOUCH_EVERY:
            sa.last_used_at = now
        return Principal(
            kind="service",
            owner_id=sa.id,
            name=sa.name,
            operations=frozenset(sa.operations or []),
            acting_user_id=acting_id,
            memory_default="on" if sa.memory_default == "on" else "off",
            agent_scope="list" if sa.agent_scope == "list" else "all_enabled",
            agent_ids=frozenset(UUID(str(a)) for a in sa.agent_ids or []),
        )
