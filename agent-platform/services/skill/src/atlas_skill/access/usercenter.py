"""UserCenterGateway：用户中心开放接口的唯一客户端（防腐层，c4-model/skill/code-access.html）。

    授权快照   GET /open/v1/users/{auth_uid}/authz：操作码、能不能用「技能」应用、所属租户

★ 以本应用的身份调（X-Caller-App: skill），透传本次请求的 X-Uid、X-Auth-Uid、X-Tenant-Id（用户中心
  据此建租户上下文）。内网免令牌。
★ 按（auth-uid, 租户）缓存成功的快照；失败（不可用、停用、不在范围）每次都重新问，恢复后立即可用。
★ 错误口径：不在这个租户 / 不存在（401、404）→ 401；账号停用 → 401；不能用「技能」应用 → 403；
  不可达、其他失败 → 503，一律不放行。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import httpx
from atlas_tenancy import TENANT_HEADER, parse_tenant

from ..errors import Forbidden, Unauthorized, Unavailable

logger = logging.getLogger(__name__)

__all__ = ["Snapshot", "UserCenterGateway"]


@dataclass(frozen=True)
class Snapshot:
    operations: frozenset[str]
    #: 用户中心给的用户所属租户
    tenant: UUID | None


class _SnapshotCache:
    """(auth-uid, 租户) → (过期时刻, 快照)。只缓存成功结果。"""

    def __init__(self, ttl_s: float) -> None:
        self._ttl = ttl_s
        self._rows: dict[tuple[str, UUID], tuple[float, Snapshot]] = {}

    def get(self, key: tuple[str, UUID]) -> Snapshot | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: tuple[str, UUID], snap: Snapshot) -> None:
        if len(self._rows) > 10_000:  # 防止无界增长
            self._rows.clear()
        self._rows[key] = (time.monotonic() + self._ttl, snap)


def _unavailable() -> Unavailable:
    return Unavailable("暂时无法校验权限，请稍后重试")


class UserCenterGateway:
    def __init__(
        self,
        base_url: str,
        *,
        app_code: str = "skill",
        snapshot_ttl_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=5.0, trust_env=False, transport=transport
        )
        self._app_code = app_code
        self._snapshots = _SnapshotCache(snapshot_ttl_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def authz(self, auth_uid: str, tenant: UUID, identity: Mapping[str, str]) -> Snapshot:
        """identity：本次请求的身份请求头（透传）。"""
        hit = self._snapshots.get((auth_uid, tenant))
        if hit is not None:
            return hit
        headers = {**identity, "X-Caller-App": self._app_code, TENANT_HEADER: str(tenant)}
        try:
            r = await self._http.get(f"/open/v1/users/{auth_uid}/authz", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("取授权快照失败（用户中心不可达）：%s", exc)
            raise _unavailable() from exc
        if r.status_code in (401, 404):
            raise Unauthorized("请先登录")
        if r.status_code != 200:
            logger.warning("授权快照返回 %s", r.status_code)
            raise _unavailable()
        body = r.json()
        user = body.get("user") or {}
        if user.get("status") == "disabled":
            raise Unauthorized("账号已停用")
        if not body.get("can_access"):
            raise Forbidden("没有使用「技能」应用的权限")
        snap = Snapshot(
            frozenset(body.get("permissions") or []), parse_tenant(user.get("tenant_id"))
        )
        self._snapshots.put((auth_uid, tenant), snap)
        return snap
