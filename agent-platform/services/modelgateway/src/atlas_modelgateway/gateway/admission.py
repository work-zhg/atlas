"""准入（c4-model/modelgateway/code-gateway-admission.html）：这个请求能不能放进来。

Key 是不是真的、还有没有效、能不能用这个模型、限额够不够。判断都在内存里做（配置快照）；
每分钟计数和本月花费放在网关状态里，几个网关实例共用。

★ 只认摘要（sha256）：Key 是随机生成的长串，普通摘要就够，慢哈希会拖慢每一次调用。
★ 私有模型的授权不在这里判：授权收回时管理平台把模型从 Key 里去掉。
★ 预算是软上限：调用前看「本月已花 < 预算」，结束后再加；同时放行的几个可能一起超出一点。
★ 网关状态不可用时不挡业务：每分钟请求数不限，预算按进程里最后一次读到的花费判断。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from .config import ConfigSnapshot, KeyEntry, ModelEntry
from .kernel import ErrorCategory, GatewayError, ModelKind

__all__ = ["Admission", "Grant", "QuotaGuard", "key_digest"]

logger = logging.getLogger(__name__)

_KIND_TEXT = {ModelKind.CHAT: "对话", ModelKind.EMBEDDING: "向量", ModelKind.RERANK: "重排"}


def key_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True)
class Grant:
    key: KeyEntry
    model: ModelEntry

    @property
    def tenant_uuid(self) -> UUID:
        return self.key.tenant_uuid


class QuotaGuard:
    """每分钟请求数（固定一分钟窗口）和本月花费。"""

    def __init__(self, redis: Any | None, *, prefix: str = "modelgw") -> None:
        self._redis = redis
        self._prefix = prefix
        self._spent: dict[tuple[UUID, str], Decimal] = {}

    @staticmethod
    def _month() -> str:
        return datetime.now(UTC).strftime("%Y%m")

    async def check(self, key: KeyEntry) -> None:
        if key.rpm and self._redis is not None:
            minute = int(time.time() // 60)
            name = f"{self._prefix}:rpm:{key.key_uuid}:{minute}"
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
        if key.monthly_budget is not None:
            spent = await self.spent(key.key_uuid)
            if spent >= key.monthly_budget:
                raise GatewayError(
                    ErrorCategory.KEY_LIMIT,
                    f"这把 Key 本月的预算 ¥{key.monthly_budget} 已经用完，下月 1 日恢复；"
                    "急用可以在 Key 里调高预算",
                )

    async def spent(self, key: UUID) -> Decimal:
        month = self._month()
        if self._redis is not None:
            try:
                raw = await self._redis.get(f"{self._prefix}:spend:{key}:{month}")
                value = (
                    Decimal(raw.decode() if isinstance(raw, bytes) else str(raw))
                    if raw
                    else Decimal(0)
                )
                self._spent[(key, month)] = value
                return value
            except Exception:
                logger.warning("网关状态不可用，预算按最后一次读到的花费判断", exc_info=True)
        return self._spent.get((key, month), Decimal(0))

    async def charge(self, key: UUID, cost: Decimal) -> None:
        if cost <= 0:
            return
        month = self._month()
        self._spent[(key, month)] = self._spent.get((key, month), Decimal(0)) + cost
        if self._redis is not None:
            name = f"{self._prefix}:spend:{key}:{month}"
            try:
                await self._redis.incrbyfloat(name, float(cost))
                await self._redis.expire(name, 40 * 86400)
            except Exception:
                logger.warning("网关状态不可用，这次花费只记在本实例", exc_info=True)


class Admission:
    def __init__(
        self,
        config: ConfigSnapshot,
        quota: QuotaGuard,
        *,
        tenant_active: Callable[[UUID], Awaitable[bool]] | None = None,
    ) -> None:
        self.config = config
        self.quota = quota
        self._tenant_active = tenant_active

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
        if self._tenant_active is not None and not await self._tenant_active(key.tenant_uuid):
            raise GatewayError(ErrorCategory.KEY_INVALID, "所在的组织已停用")
        return key

    async def admit(self, secret: str | None, model: str, kind: ModelKind) -> Grant:
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
        await self.quota.check(key)
        return Grant(key, entry)

    async def models_of(self, secret: str | None) -> list[ModelEntry]:
        key = await self.authenticate(secret)
        config = await self.config.tenant(key.tenant_uuid)
        return [m for name, m in sorted(config.models.items()) if name in key.models and m.enabled]

    async def settle(self, grant: Grant, cost: Decimal) -> None:
        try:
            await self.quota.charge(grant.key.key_uuid, cost)
        except Exception:
            logger.exception("累加花费失败")
