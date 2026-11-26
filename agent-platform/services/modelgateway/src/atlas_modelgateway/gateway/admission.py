"""准入（c4-model/modelgateway/code-gateway-admission.html）：这个请求能不能放进来。

Key 是不是真的、还有没有效、能不能用这个模型、限额够不够。判断都在内存里做（配置快照）；
每分钟计数和本月花费放在网关状态里，几个网关实例共用。

★ 只认摘要（sha256）：Key 是随机生成的长串，普通摘要就够，慢哈希会拖慢每一次调用。
★ 先判静态权限、月预算、模型能力与格式，全部通过后才扣减每分钟请求数（RPM），避免本地被拒的
  400/403/预算超限白白消耗调用方的每分钟配额。
★ 本月花费懒重建：Redis 重启或键缺失时，按 (租户, Key, 月份) 单飞从平台库 gw_model_call 汇总回填，
  既不在热路径每次查库，也不因 Redis 重启把当月预算清零。
★ 网关状态不可用时不挡业务：每分钟请求数不限，预算按进程里最后一次读到的花费判断。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from atlas_redis import AtomicQuota, RedisClient, ScriptRegistry
from atlas_tenancy import TenantScope

from ..shared.kernel import ErrorCategory, GatewayError, ModelKind, key_digest
from .config import ConfigSnapshot, KeyEntry, ModelEntry

__all__ = ["Admission", "Grant", "QuotaGuard", "SpendLoader", "key_digest"]

logger = logging.getLogger(__name__)

_KIND_TEXT = {ModelKind.CHAT: "对话", ModelKind.EMBEDDING: "向量", ModelKind.RERANK: "重排"}
_SPEND_TTL_S = 40 * 86400

SpendLoader = Callable[[UUID, UUID, datetime], Awaitable[Decimal]]


@dataclass(frozen=True)
class Grant:
    key: KeyEntry
    model: ModelEntry

    @property
    def tenant_uuid(self) -> UUID:
        return self.key.tenant_uuid


class QuotaGuard:
    """每分钟请求数（固定一分钟窗口）和本月花费。

    ★ 接入 atlas_redis.RedisClient 时走 AtomicQuota（单条 Lua 原子 INCR+EXPIRE / INCRBYFLOAT+EXPIRE
      与 Fail-Open 容灾），彻底消除两条命令之间崩溃导致的僵尸限流键。
      测试传入原始假客户端时保留兼容路径。
    """

    def __init__(
        self,
        redis: Any | None,
        *,
        prefix: str = "modelgw",
        quota: AtomicQuota | None = None,
        spend_loader: SpendLoader | None = None,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._spend_loader = spend_loader
        self._spent: dict[tuple[UUID, str], Decimal] = {}
        self._backfilled: set[tuple[UUID, str]] = set()
        self._inflight_spend: dict[tuple[UUID, str], asyncio.Future[Decimal]] = {}
        if quota is not None:
            self._quota: AtomicQuota | None = quota
        elif isinstance(redis, RedisClient):
            self._quota = AtomicQuota(commands=redis, scripts=ScriptRegistry(redis), fail_open=True)
        else:
            self._quota = None

    @staticmethod
    def _month_window() -> tuple[str, datetime]:
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return now.strftime("%Y%m"), start

    @classmethod
    def _month(cls) -> str:
        return cls._month_window()[0]

    async def check_budget(self, key: KeyEntry) -> None:
        """只查月度预算（不扣减 RPM）。"""
        if key.monthly_budget is None:
            return
        spent = await self.spent(key.key_uuid, tenant_uuid=key.tenant_uuid)
        if spent >= key.monthly_budget:
            raise GatewayError(
                ErrorCategory.KEY_LIMIT,
                f"这把 Key 本月的预算 ¥{key.monthly_budget} 已经用完，下月 1 日恢复；"
                "急用可以在 Key 里调高预算",
            )

    async def consume_rpm(self, key: KeyEntry) -> None:
        """在准入、能力与入口格式全部检查通过后扣减一次 RPM。"""
        if not key.rpm or self._redis is None:
            return
        minute = int(time.time() // 60)
        name = f"{self._prefix}:rpm:{key.key_uuid}:{minute}"
        if self._quota is not None:
            res = await self._quota.allow(
                name, limit=key.rpm, window_s=120, key_pattern="modelgw:rpm:*"
            )
            if not res.allowed:
                wait = 60 - int(time.time()) % 60
                raise GatewayError(
                    ErrorCategory.KEY_LIMIT,
                    f"这把 Key 每分钟最多 {key.rpm} 次请求，{wait} 秒后恢复",
                    retry_after_s=wait,
                )
            return
        try:
            count = await self._redis.incr(name)
            if count == 1:
                await self._redis.expire(name, 120)
        except Exception:
            logger.warning("网关状态不可用，这次不数每分钟请求数", exc_info=True)
            count = 0
        if count > key.rpm:
            wait = 60 - int(time.time()) % 60
            raise GatewayError(
                ErrorCategory.KEY_LIMIT,
                f"这把 Key 每分钟最多 {key.rpm} 次请求，{wait} 秒后恢复",
                retry_after_s=wait,
            )

    async def check(self, key: KeyEntry) -> None:
        """先判预算再扣 RPM（供直接单测或一次性校验调用）。"""
        await self.check_budget(key)
        await self.consume_rpm(key)

    async def spent(self, key: UUID, *, tenant_uuid: UUID | None = None) -> Decimal:
        month, month_start = self._month_window()
        slot = (key, month)
        if self._redis is not None:
            name = f"{self._prefix}:spend:{key}:{month}"
            try:
                raw = await self._redis.get(name)
            except Exception:
                logger.warning("网关状态不可用，预算按最后一次读到的花费判断", exc_info=True)
                return self._spent.get(slot, Decimal(0))
            if raw is not None:
                value = Decimal(raw.decode() if isinstance(raw, bytes) else str(raw))
                self._spent[slot] = value
                self._backfilled.add(slot)
                return value
            # Redis 里没有该键（新月份、新 Key 或 Redis 重启）：有回源器时从调用记录懒重建一次
            if self._spend_loader is not None and tenant_uuid is not None:
                return await self._backfill_spend(slot, tenant_uuid, month_start, redis_key=name)
            self._spent[slot] = Decimal(0)
            return Decimal(0)
        if (
            slot not in self._backfilled
            and self._spend_loader is not None
            and tenant_uuid is not None
        ):
            return await self._backfill_spend(slot, tenant_uuid, month_start, redis_key=None)
        return self._spent.get(slot, Decimal(0))

    async def _backfill_spend(
        self,
        slot: tuple[UUID, str],
        tenant_uuid: UUID,
        month_start: datetime,
        *,
        redis_key: str | None,
    ) -> Decimal:
        existing = self._inflight_spend.get(slot)
        if existing is not None:
            return await asyncio.shield(existing)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decimal] = loop.create_future()
        self._inflight_spend[slot] = fut
        try:
            assert self._spend_loader is not None
            with TenantScope.isolated():
                loaded = await self._spend_loader(tenant_uuid, slot[0], month_start)
            value = max(loaded, self._spent.get(slot, Decimal(0)))
            self._spent[slot] = value
            self._backfilled.add(slot)
            if redis_key is not None and self._redis is not None and hasattr(self._redis, "set"):
                try:
                    ok = await self._redis.set(redis_key, str(value), nx=True, ex=_SPEND_TTL_S)
                    if not ok:
                        raw = await self._redis.get(redis_key)
                        if raw is not None:
                            cur = Decimal(raw.decode() if isinstance(raw, bytes) else str(raw))
                            self._spent[slot] = value = max(cur, value)
                except Exception:
                    logger.warning("回填当月花费到网关状态失败", exc_info=True)
            if not fut.done():
                fut.set_result(value)
            return value
        except Exception:
            logger.warning("从平台库重建当月花费失败，暂按内存已知值判断", exc_info=True)
            fallback = self._spent.get(slot, Decimal(0))
            if not fut.done():
                fut.set_result(fallback)
            return fallback
        finally:
            if self._inflight_spend.get(slot) is fut:
                self._inflight_spend.pop(slot, None)

    async def charge(self, key: UUID, cost: Decimal, *, tenant_uuid: UUID | None = None) -> None:
        if cost <= 0:
            return
        month, _ = self._month_window()
        slot = (key, month)
        if self._redis is not None:
            name = f"{self._prefix}:spend:{key}:{month}"
            # 若本进程还未确认过 Redis 里的基数且配了回源器，先确保不会在 0 上盲加覆盖掉月初已花金额
            if slot not in self._backfilled and self._spend_loader is not None and tenant_uuid:
                await self.spent(key, tenant_uuid=tenant_uuid)
            self._spent[slot] = self._spent.get(slot, Decimal(0)) + cost
            self._backfilled.add(slot)
            if self._quota is not None:
                updated = await self._quota.add_spend(
                    name, cost, ttl_s=_SPEND_TTL_S, key_pattern="modelgw:spend:*"
                )
                if updated is not None:
                    self._spent[slot] = updated
                return
            try:
                await self._redis.incrbyfloat(name, float(cost))
                await self._redis.expire(name, _SPEND_TTL_S)
            except Exception:
                logger.warning("网关状态不可用，这次花费只记在本实例", exc_info=True)
            return
        if slot not in self._backfilled and self._spend_loader is not None and tenant_uuid:
            await self.spent(key, tenant_uuid=tenant_uuid)
        self._spent[slot] = self._spent.get(slot, Decimal(0)) + cost
        self._backfilled.add(slot)


class Admission:
    def __init__(self, config: ConfigSnapshot, quota: QuotaGuard) -> None:
        self.config = config
        self.quota = quota

    async def authenticate(self, secret: str | None) -> KeyEntry:
        if not secret:
            raise GatewayError(ErrorCategory.KEY_INVALID, "缺少 API Key")
        key = await self.config.key_by_digest(key_digest(secret))
        if key is None:
            raise GatewayError(ErrorCategory.KEY_INVALID, "API Key 无效")
        if key.status == "blocked":
            raise GatewayError(ErrorCategory.KEY_INVALID, "这把 Key 已停用")
        if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
            raise GatewayError(ErrorCategory.KEY_INVALID, "这把 Key 已过期")
        return key

    async def admit(
        self,
        secret: str | None,
        model: str,
        kind: ModelKind,
        *,
        consume_rpm: bool = True,
    ) -> Grant:
        key = await self.authenticate(secret)
        if key.status == "pending" or not key.models:
            raise GatewayError(
                ErrorCategory.MODEL_FORBIDDEN, "这把 Key 还没有分配模型，请联系模型管理员"
            )
        if model not in key.models:
            raise GatewayError(
                ErrorCategory.MODEL_FORBIDDEN, f"这把 Key 不能用模型 {model}，请换一个已分配的模型"
            )
        entry = await self.config.model(key.tenant_uuid, model)
        if entry is None:
            raise GatewayError(ErrorCategory.MODEL_FORBIDDEN, f"模型 {model} 不存在")
        if not entry.enabled:
            raise GatewayError(ErrorCategory.MODEL_FORBIDDEN, f"模型 {model} 已停用")
        if entry.kind is not kind:
            raise GatewayError(
                ErrorCategory.BEYOND_CAPABILITY,
                f"{model} 是{_KIND_TEXT[entry.kind]}模型，不能用来做{_KIND_TEXT[kind]}",
            )
        await self.quota.check_budget(key)
        if consume_rpm:
            await self.quota.consume_rpm(key)
        return Grant(key, entry)

    async def consume_rpm(self, grant: Grant) -> None:
        await self.quota.consume_rpm(grant.key)

    async def models_of(self, secret: str | None) -> list[ModelEntry]:
        key = await self.authenticate(secret)
        config = await self.config.tenant(key.tenant_uuid)
        return [m for name, m in sorted(config.models.items()) if name in key.models and m.enabled]

    async def settle(self, grant: Grant, cost: Decimal) -> None:
        try:
            await self.quota.charge(grant.key.key_uuid, cost, tenant_uuid=grant.tenant_uuid)
        except Exception:
            logger.exception("累加花费失败")
