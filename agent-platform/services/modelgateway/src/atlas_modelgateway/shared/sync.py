"""配置版本同步与即时失效协议（c4-model/modelgateway/code-gateway-config.html）。

★ 按租户隔离版本号，彻底消除「单租户改配置触发全集群所有租户重加载与重解密」的广播风暴：
    modelgw:config:version          全局兜底版本（无租户调用 bump 时递增，兼容旧调用）
    modelgw:config:keys             Key 纪元版本（改 Key / 授权时递增）
    modelgw:config:tenant:<uuid>    单租户配置版本（改账号 / 模型 / 线路时只递增本租户）
★ 即时失效广播（Pub/Sub）：
    atlas:modelgw:config:changed    携带变更租户、是否涉及 Key、需立即驱逐的 Key 摘要列表，
                                    所有网关副本收到后立刻清掉被停用/删除/重置的 Key 缓存。
    atlas:modelgw:sys:tenant:changed 运营中心停用/恢复租户时广播驱逐本地租户门禁缓存。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

__all__ = [
    "CONFIG_INVALIDATE_CHANNEL",
    "KEY_VERSION_KEY",
    "TENANT_VERSION_PREFIX",
    "VERSION_KEY",
    "ConfigInvalidation",
    "VersionWatch",
]

logger = logging.getLogger(__name__)

VERSION_KEY = "modelgw:config:version"
KEY_VERSION_KEY = "modelgw:config:keys"
TENANT_VERSION_PREFIX = "modelgw:config:tenant:"
CONFIG_INVALIDATE_CHANNEL = "atlas:modelgw:config:changed"


def _decode(raw: Any) -> str:
    if raw is None:
        return "0"
    return raw.decode() if isinstance(raw, bytes) else str(raw)


@dataclass(frozen=True)
class ConfigInvalidation:
    tenant_uuid: UUID | None = None
    tenant_changed: bool = False
    keys_changed: bool = False
    evict_digests: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(
            {
                "tenant": str(self.tenant_uuid) if self.tenant_uuid else None,
                "tenant_changed": self.tenant_changed,
                "keys_changed": self.keys_changed,
                "evict_digests": list(self.evict_digests),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> ConfigInvalidation | None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        tenant_raw = data.get("tenant")
        tenant: UUID | None = None
        if tenant_raw:
            try:
                tenant = UUID(str(tenant_raw))
            except ValueError:
                tenant = None
        digests = tuple(str(d) for d in (data.get("evict_digests") or []) if d)
        return cls(
            tenant_uuid=tenant,
            tenant_changed=bool(data.get("tenant_changed")),
            keys_changed=bool(data.get("keys_changed")),
            evict_digests=digests,
        )


class VersionWatch:
    """配置版本号观察器：一次 mget 同时读取全局版本、Key 版本和本实例已缓存租户的独立版本。

    网关状态不可用时退回按时间：每 fallback_s 秒「变一次」，也就是定期重新加载。
    """

    def __init__(
        self, redis: Any | None, *, check_s: float = 1.0, fallback_s: float = 30.0
    ) -> None:
        self._redis = redis
        self._check_s = check_s
        self._fallback_s = fallback_s
        self._global = "0"
        self._keys_ver = "0"
        self._tenant_vers: dict[UUID, str] = {}
        self._checked = 0.0

    def invalidate_poll(self) -> None:
        """收到 Pub/Sub 广播后让下一次读取立刻拉最新版本号，不等 check_s 窗口。"""
        self._checked = 0.0

    async def _refresh(self, tenants: Sequence[UUID] = ()) -> None:
        now = time.monotonic()
        need_new_tenant = any(t not in self._tenant_vers for t in tenants)
        if not need_new_tenant and now - self._checked < self._check_s:
            return
        self._checked = now
        if self._redis is None:
            tick = f"t{int(time.time() // self._fallback_s)}"
            self._global = tick
            self._keys_ver = tick
            for t in tenants:
                self._tenant_vers[t] = tick
            return
        try:
            if hasattr(self._redis, "mget"):
                names = [
                    VERSION_KEY,
                    KEY_VERSION_KEY,
                    *(f"{TENANT_VERSION_PREFIX}{t}" for t in tenants),
                ]
                vals = await self._redis.mget(names)
                self._global = _decode(vals[0])
                self._keys_ver = _decode(vals[1])
                for t, raw in zip(tenants, vals[2:], strict=True):
                    self._tenant_vers[t] = _decode(raw)
            else:
                self._global = _decode(await self._redis.get(VERSION_KEY))
                self._keys_ver = _decode(await self._redis.get(KEY_VERSION_KEY))
                for t in tenants:
                    self._tenant_vers[t] = _decode(
                        await self._redis.get(f"{TENANT_VERSION_PREFIX}{t}")
                    )
        except Exception:
            logger.warning("读不到配置版本号，按时间重新加载", exc_info=True)
            tick = f"t{int(time.time() // self._fallback_s)}"
            self._global = tick
            self._keys_ver = tick
            for t in tenants:
                self._tenant_vers[t] = tick

    async def current(self) -> str:
        """全局 + Key 复合版本号（供 Key 认证与兼容调用）。"""
        await self._refresh()
        return f"{self._global}:{self._keys_ver}"

    async def key_version(self, known_tenants: Sequence[UUID] = ()) -> str:
        await self._refresh(known_tenants)
        return f"{self._global}:{self._keys_ver}"

    async def tenant_version(self, tenant_uuid: UUID, known_tenants: Sequence[UUID] = ()) -> str:
        targets = list(dict.fromkeys([*known_tenants, tenant_uuid]))
        await self._refresh(targets)
        return f"{self._global}:{self._tenant_vers.get(tenant_uuid, '0')}"

    @staticmethod
    async def bump(
        redis: Any | None,
        *,
        tenant_uuid: UUID | None = None,
        tenant_changed: bool = False,
        keys_changed: bool = False,
        evict_digests: Iterable[str] = (),
        pubsub: Any | None = None,
    ) -> None:
        """管理平台改完配置调用：按租户或 Key 精细递增，并广播即时失效。"""
        if redis is None:
            return
        digests = tuple(dict.fromkeys(d for d in evict_digests if d))
        if tenant_uuid is None and not tenant_changed and not keys_changed and not digests:
            # 兼容直接调 VersionWatch.bump(redis) 的无参用法
            await redis.incr(VERSION_KEY)
            return
        if tenant_changed:
            if tenant_uuid is not None:
                await redis.incr(f"{TENANT_VERSION_PREFIX}{tenant_uuid}")
            else:
                await redis.incr(VERSION_KEY)
        if keys_changed or digests:
            await redis.incr(KEY_VERSION_KEY)
        event = ConfigInvalidation(
            tenant_uuid=tenant_uuid,
            tenant_changed=tenant_changed,
            keys_changed=keys_changed or bool(digests),
            evict_digests=digests,
        )
        payload = event.to_json()
        if pubsub is not None and hasattr(pubsub, "publish"):
            await pubsub.publish(CONFIG_INVALIDATE_CHANNEL, payload)
        elif hasattr(redis, "publish"):
            await redis.publish(CONFIG_INVALIDATE_CHANNEL, payload)
