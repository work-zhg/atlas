"""授权快照与用户投影（权限设计 §4、§5；内网身份传递 §3、§6）。

★ 登录、会话 Cookie、CSRF 都在网关 + 用户中心；本服务只按请求头里的 uid / auth-uid 取授权快照。
★ 授权快照（操作码 / 菜单 / 能否访问）按用户进程内缓存 authz_snapshot_ttl_s；
  多副本时撤销经 Redis 广播（authz:invalidate），各实例清本地缓存；TTL 仍是兜底。
★ 角色不存库：来自用户中心快照（存一份就会出现「用户中心已收回、Atlas 还写着有」）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import AppUser
from ..errors import Forbidden, Unauthorized
from .principal import Principal
from .uc import UCClient, UCError

__all__ = [
    "AuthService",
    "data_level_cache",
    "listen_invalidations",
    "snapshot_cache",
]

log = logging.getLogger("atlas_agent.auth")


#: 最近访问时间：最多这么久写一次库，避免每个请求都写
_TOUCH_EVERY = timedelta(minutes=10)


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
#: 数据编码级授权（数据权限领域，与上面的角色快照分开）：用户 id → {数据编码: 级别}
data_level_cache = _SnapshotCache()

#: 授权快照失效的广播频道（横向扩展设计 §7.3）。消息体 = 用户 id
AUTHZ_INVALIDATE_CHANNEL = "authz:invalidate"


async def listen_invalidations(settings: Settings) -> None:
    """常驻：订阅失效广播，清本进程的快照缓存。由 lifespan 起停，断了退避重连。"""
    from ..redisx import make_redis

    delay = 1.0
    while True:
        redis = make_redis(settings)
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(AUTHZ_INVALIDATE_CHANNEL)
            delay = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                with contextlib.suppress(ValueError):
                    uid = UUID(str(message.get("data")))
                    snapshot_cache.drop(uid)
                    data_level_cache.drop(uid)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("授权失效订阅断开，%.0fs 后重连", delay, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)


#: 部门 id → 上级部门 id（用户中心部门树；展开文档级权限的「含上级部门」用）
_dept_parents: tuple[float, dict[str, str | None]] = (0.0, {})


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

    # ───────────────────────────── 请求主体

    async def resolve(
        self, uid: UUID, auth_uid: UUID, *, caller_app: str, tenant_id: str | None
    ) -> Principal:
        """请求头身份 → 主体。操作码、数据编码级授权按 auth_uid（按谁的权限判）取。"""
        snap = await self._snapshot(auth_uid)
        if snap.get("disabled"):
            raise Unauthorized("账号已停用")
        if not snap.get("can_access"):
            raise Forbidden("没有使用 Atlas 的权限，请联系管理员在用户中心授权")
        user = await self.session.get(AppUser, uid)
        if user is not None and (
            user.last_seen_at is None or _now() - user.last_seen_at > _TOUCH_EVERY
        ):
            user.last_seen_at = _now()
        return Principal(
            uid=uid,
            auth_uid=auth_uid,
            caller_app=caller_app,
            tenant_id=tenant_id,
            name=user.name if user is not None else "",
            operations=frozenset(snap.get("permissions") or []),
            menus=tuple(snap.get("menus") or []),
            data_levels=await self._data_levels(auth_uid),
        )

    async def _snapshot(self, user_id: UUID) -> dict[str, Any]:
        """用户中心授权快照（缓存）；顺带按用户中心资料刷新本地投影（名字、部门、状态）。"""
        snap = snapshot_cache.get(user_id)
        if snap is not None:
            return snap
        uc = self._uc()
        snap = dict(await uc.authz(user_id))
        try:
            user = await self.upsert_user(await uc.user(user_id))
            snap["disabled"] = user.status != "active"
        except UCError as exc:
            if exc.status_code != 404:
                raise
            snap["disabled"] = True
        snapshot_cache.put(user_id, snap, self.settings.authz_snapshot_ttl_s)
        return snap

    async def _data_levels(self, user_id: UUID) -> dict[str, str]:
        """用户对 Atlas 各数据编码下全部数据的级别（用户中心的数据编码级授权）。

        ★ 取不到时按无（NONE）处理且不缓存：它只放大权限（看 / 管全部），缺了只是退回
          按单条数据判定，不该让请求失败。
        """
        from .catalog import DATA_TYPE_CODES

        cached = data_level_cache.get(user_id)
        if cached is not None:
            return cached
        uc = self._uc()
        levels: dict[str, str] = {}
        try:
            for code in DATA_TYPE_CODES:
                levels[code] = str((await uc.data_type_level(user_id, code)).get("permission"))
        except UCError:
            log.warning("取数据编码级授权失败，按无处理 user=%s", user_id, exc_info=True)
            return {}
        data_level_cache.put(user_id, levels, self.settings.authz_snapshot_ttl_s)
        return levels

    # ───────────────────────────── 文档级权限的主体展开（知识库 §7.4）

    async def principal_tags(self, user_id: UUID) -> list[str]:
        """用户 → 主体标签：u:用户、d:所在部门及各级上级、r:角色（Atlas 应用内）。

        知识库检索拿它和文档的 acl 求交集。未接入用户中心（dev）时只有 u:。
        """
        tags = [f"u:{user_id}"]
        if self.uc is None:
            return tags
        snap = await self._snapshot(user_id)
        user = await self.session.get(AppUser, user_id)
        if user is None:
            return tags
        tags += [f"r:{r}" for r in snap.get("roles") or []]
        if user.dept_id:
            parents = await self._dept_parents()
            dept: str | None = str(user.dept_id)
            seen: set[str] = set()
            while dept and dept not in seen:
                seen.add(dept)
                tags.append(f"d:{dept}")
                dept = parents.get(dept)
        return tags

    async def _dept_parents(self) -> dict[str, str | None]:
        global _dept_parents
        expires, parents = _dept_parents
        if expires > time.monotonic():
            return parents
        rows = await self._uc().depts_tree()
        parents = {
            str(d["id"]): (str(d["parent_id"]) if d.get("parent_id") else None) for d in rows
        }
        _dept_parents = (time.monotonic() + self.settings.authz_snapshot_ttl_s, parents)
        return parents
