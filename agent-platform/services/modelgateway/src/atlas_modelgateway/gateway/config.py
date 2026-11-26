"""配置快照（c4-model/modelgateway/code-gateway-config.html）：调用路径上不查库。

账号、模型、线路、Key 由管理平台写，网关只读一份内存副本；管理平台改完按租户递增配置版本号并通过
Pub/Sub 广播即时失效。

★ 按租户分开加载与按租户独立版本号：
    认 Key       按全局唯一的摘要只查那一行（SystemReason.CREDENTIAL），查到就知道是哪个租户；
                 Key 版本变化或收到停用/删除/重置广播时立即驱逐并单飞回源，绝不放行已吊销的 Key
    租户的配置   进入这个租户，加载它的模型、线路、账号；租户 A 改配置只刷新 A，别的租户不重拉解密
★ 平台库暂时不可用时，用内存里最后一份继续服务；新改的配置等恢复后才生效。
★ 账号密钥只在加载时解密，明文只在内存里。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from atlas_redis import PubSubHub, Resubscribed
from atlas_tenancy import SystemReason, TenantScope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..crypto import CipherError, SecretCipher
from ..db.models import GwAccount, GwApiKey, GwModel, GwRoute
from ..shared.kernel import AccountRef, Egress, Format, ModelCaps, ModelKind, Price, Route
from ..shared.sync import (
    CONFIG_INVALIDATE_CHANNEL,
    VERSION_KEY,
    ConfigInvalidation,
    VersionWatch,
)

__all__ = [
    "VERSION_KEY",
    "ConfigSnapshot",
    "KeyEntry",
    "ModelEntry",
    "TenantConfig",
    "VersionWatch",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeyEntry:
    key_uuid: UUID
    tenant_uuid: UUID
    kind: str  # user / client
    status: str  # pending / active / blocked
    #: 这把 Key 此刻能用的模型（管理平台按授权算好）
    models: frozenset[str]
    name: str = ""
    expires_at: datetime | None = None
    monthly_budget: Decimal | None = None
    rpm: int | None = None


@dataclass(frozen=True)
class ModelEntry:
    uuid: UUID
    tenant_uuid: UUID
    name: str
    display_name: str
    kind: ModelKind
    enabled: bool
    caps: ModelCaps
    price: Price
    routes: tuple[Route, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class TenantConfig:
    tenant_uuid: UUID
    version: str
    models: dict[str, ModelEntry] = field(default_factory=dict)


@dataclass
class _Cached:
    value: Any
    version: str
    at: float


class ConfigSnapshot:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        cipher: SecretCipher,
        watch: VersionWatch,
        *,
        pubsub: PubSubHub | None = None,
        negative_ttl_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sm = sessionmaker
        self._cipher = cipher
        self._watch = watch
        self._pubsub = pubsub
        self._negative_ttl = negative_ttl_s
        self._clock = clock
        self._keys: dict[str, _Cached] = {}
        self._tenants: dict[UUID, _Cached] = {}
        self._inflight_keys: dict[str, asyncio.Future[KeyEntry | None]] = {}
        self._reloading: set[Any] = set()
        self._listener: asyncio.Task[None] | None = None

    # ── 跨副本即时失效监听 ─────────────────────────────────────────────

    def start_invalidation_listener(self) -> None:
        if self._pubsub is None:
            return
        if self._listener is not None and not self._listener.done():
            return
        with TenantScope.isolated():
            self._listener = asyncio.create_task(self._listen(), name="modelgw-config-invalidation")

    async def stop_invalidation_listener(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
            self._listener = None

    async def _listen(self) -> None:
        assert self._pubsub is not None
        async with self._pubsub.subscribe(CONFIG_INVALIDATE_CHANNEL) as queue:
            while True:
                msg = await queue.get()
                if isinstance(msg, Resubscribed):
                    # 断线重连期间可能漏掉停用广播：清空 Key 缓存并立刻重拉版本号
                    self._keys.clear()
                    self._watch.invalidate_poll()
                    continue
                event = ConfigInvalidation.from_json(msg)
                if event is None:
                    continue
                for digest in event.evict_digests:
                    self._keys.pop(digest, None)
                self._watch.invalidate_poll()
                if (
                    event.tenant_changed
                    and event.tenant_uuid is not None
                    and event.tenant_uuid in self._tenants
                ):
                    tid = event.tenant_uuid
                    self._background(("tenant", tid), self._refresh_tenant_now(tid))

    async def _refresh_tenant_now(self, tenant_uuid: UUID) -> None:
        version = await self._watch.tenant_version(tenant_uuid, tuple(self._tenants))
        await self._reload_tenant(tenant_uuid, version)

    # ── Key ────────────────────────────────────────────────────────────

    def evict_key(self, digest: str) -> None:
        """立即驱逐单把 Key 的内存缓存。"""
        self._keys.pop(digest, None)

    async def key_by_digest(self, digest: str) -> KeyEntry | None:
        version = await self._watch.key_version(tuple(self._tenants))
        hit = self._keys.get(digest)
        if hit is not None:
            if (
                hit.value is None
                and self._clock() - hit.at < self._negative_ttl
                and hit.version == version
            ):
                return None
            if hit.value is not None and hit.version == version:
                return hit.value
        # 版本已变或未命中：按 digest 单飞同步重查这一行，杜绝已停用 / 已收回权限的 Key 被旧缓存放行
        existing = self._inflight_keys.get(digest)
        if existing is not None:
            return await asyncio.shield(existing)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[KeyEntry | None] = loop.create_future()
        self._inflight_keys[digest] = fut
        try:
            with TenantScope.isolated():
                result = await self._reload_key(digest, version)
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
                fut.exception()
            raise
        else:
            if not fut.done():
                fut.set_result(result)
            return result
        finally:
            if self._inflight_keys.get(digest) is fut:
                self._inflight_keys.pop(digest, None)

    async def _reload_key(self, digest: str, version: str) -> KeyEntry | None:
        try:
            entry = await self._load_key(digest)
        except Exception:
            stale = self._keys.get(digest)
            if stale is not None and stale.value is not None:
                logger.warning("重新加载 Key 失败，沿用上一份", exc_info=True)
                return stale.value
            raise
        if len(self._keys) > 100_000:
            self._keys.clear()
        self._keys[digest] = _Cached(entry, version, self._clock())
        return entry

    async def _load_key(self, digest: str) -> KeyEntry | None:
        # 还不知道是哪个租户：按全局唯一的摘要只查这一行
        with TenantScope.system(SystemReason.CREDENTIAL):
            async with self._sm() as session:
                row = await session.scalar(select(GwApiKey).where(GwApiKey.secret_digest == digest))
                if row is None:
                    # 客户端 Key 重置后，旧的再保留一会儿
                    row = await session.scalar(
                        select(GwApiKey).where(
                            GwApiKey.prev_digest == digest, GwApiKey.prev_until > datetime.now(UTC)
                        )
                    )
        if row is None:
            return None
        return KeyEntry(
            key_uuid=row.uuid,
            tenant_uuid=row.tenant_uuid,
            kind=row.key_type,
            status=row.status,
            models=frozenset(row.effective_models or []),
            name=row.label or row.name,
            expires_at=row.expires_at,
            monthly_budget=row.monthly_budget,
            rpm=row.rpm,
        )

    # ── 租户的模型、线路、账号 ───────────────────────────────────────────

    async def tenant(self, tenant_uuid: UUID) -> TenantConfig:
        version = await self._watch.tenant_version(tenant_uuid, tuple(self._tenants))
        hit = self._tenants.get(tenant_uuid)
        if hit is not None:
            if hit.version != version:
                self._background(("tenant", tenant_uuid), self._reload_tenant(tenant_uuid, version))
            return hit.value
        return await self._reload_tenant(tenant_uuid, version)

    async def model(self, tenant_uuid: UUID, name: str) -> ModelEntry | None:
        return (await self.tenant(tenant_uuid)).models.get(name)

    async def _reload_tenant(self, tenant_uuid: UUID, version: str) -> TenantConfig:
        try:
            config = await self._load_tenant(tenant_uuid, version)
        except Exception:
            stale = self._tenants.get(tenant_uuid)
            if stale is not None:
                logger.warning("重新加载租户 %s 的配置失败，沿用上一份", tenant_uuid, exc_info=True)
                return stale.value
            raise
        self._tenants[tenant_uuid] = _Cached(config, version, self._clock())
        return config

    async def _load_tenant(self, tenant_uuid: UUID, version: str) -> TenantConfig:
        with TenantScope.enter(tenant_uuid):
            async with self._sm() as session:
                models = list(await session.scalars(select(GwModel)))
                routes = list(await session.scalars(select(GwRoute)))
                accounts = {a.uuid: a for a in await session.scalars(select(GwAccount))}
        refs = {u: self._account(a) for u, a in accounts.items()}
        by_model: dict[UUID, list[Route]] = {}
        for r in routes:
            account = refs.get(r.account_uuid)
            if account is None:
                continue
            by_model.setdefault(r.model_uuid, []).append(
                Route(
                    uuid=r.uuid,
                    model="",
                    account=account,
                    upstream_model=r.upstream_model,
                    priority=r.priority,
                    weight=r.weight,
                )
            )
        entries: dict[str, ModelEntry] = {}
        for m in models:
            caps = ModelCaps.from_json(m.caps)
            entries[m.name] = ModelEntry(
                uuid=m.uuid,
                tenant_uuid=m.tenant_uuid,
                name=m.name,
                display_name=m.display_name or m.name,
                kind=ModelKind(m.kind),
                enabled=m.enabled,
                caps=replace(caps, kind=ModelKind(m.kind)),
                price=Price.from_json(m.price),
                routes=tuple(
                    Route(r.uuid, m.name, r.account, r.upstream_model, r.priority, r.weight)
                    for r in by_model.get(m.uuid, [])
                ),
                description=m.description,
            )
        return TenantConfig(tenant_uuid, version, entries)

    def _account(self, row: GwAccount) -> AccountRef:
        secret, enabled = "", row.enabled
        if row.secret_cipher:
            try:
                secret = self._cipher.decrypt(row.secret_cipher)
            except CipherError:
                logger.error("账号 %s（%s）的密钥解不开，先不用它", row.name, row.uuid)
                enabled = False
        known = {f.value for f in Format}
        endpoints = {Format(k): v for k, v in (row.endpoints or {}).items() if v and k in known}
        return AccountRef(
            uuid=row.uuid,
            provider=row.provider,
            endpoints=endpoints,
            secret=secret,
            egress=Egress(row.egress),
            first_content_timeout_s=float(row.first_content_timeout_s)
            if row.first_content_timeout_s
            else None,
            enabled=enabled,
        )

    # ── 后台重新加载 ────────────────────────────────────────────────────

    def _background(self, key: Any, coro: Any) -> None:
        if key in self._reloading:
            coro.close()
            return
        self._reloading.add(key)

        async def run() -> None:
            try:
                await coro
            except Exception:
                logger.warning("后台重新加载配置失败", exc_info=True)
            finally:
                self._reloading.discard(key)

        # 后台任务不继承请求的租户上下文：加载自己会进入需要的范围
        with TenantScope.isolated():
            asyncio.get_running_loop().create_task(run())

    def forget(self) -> None:
        """测试用：清空缓存并让下一次读取重新拉取版本号。"""
        self._keys.clear()
        self._tenants.clear()
        self._watch.invalidate_poll()
