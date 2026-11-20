"""计量（c4-model/modelgateway/code-gateway-metering.html）：花了多少钱，记一条调用记录。

★ 以供应商回答里给的用量为准，网关不自己数 token；最后一段没收到时才估算，并且标出来。
★ 价格挂在模型上（对内统一价），记录里存下当时的单价；缓存写入单独记数、按输入价算钱。
★ 调用记录先放进内存队列，后台批量写库，不让调用方多等；写不进去就攒着，攒满了丢最旧的并记数。
★ 不存请求和回答的内容。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import GwModelCall
from .kernel import Price, Usage

__all__ = ["CallRecord", "CostCalculator", "Metering", "PartitionKeeper", "RecordQueue"]

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)
_SIX = Decimal("0.000001")
#: 估算 token：输出按字符数 / 3
_CHARS_PER_TOKEN = 3


class CostCalculator:
    @staticmethod
    def cost(usage: Usage, price: Price) -> Decimal:
        if not price.billable:
            return Decimal(0)
        total = (
            Decimal(usage.input + usage.cache_write) * price.input
            + Decimal(usage.cache_hit) * price.cache_hit
            + Decimal(usage.output) * price.output
        )
        return (total / _MILLION).quantize(_SIX, rounding=ROUND_HALF_UP)


class UsageEstimator:
    """最后一段没收到时估算：已知的（比如开头给的输入用量）照用，缺的按内容长度估。"""

    @staticmethod
    def estimate(known: Usage, *, input_tokens: int, output_chars: int) -> Usage:
        return Usage(
            input=known.input or input_tokens,
            cache_write=known.cache_write,
            cache_hit=known.cache_hit,
            output=known.output or output_chars // _CHARS_PER_TOKEN,
            estimated=True,
        )


@dataclass
class CallRecord:
    call_uuid: UUID
    tenant_uuid: UUID
    started_at: datetime
    model: str
    entry_format: str
    status: str  # ok / error / interrupted
    duration_ms: int
    key_uuid: UUID | None = None
    key_kind: str = ""
    session_key: str | None = None
    error_category: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    first_content_ms: int | None = None
    usage: Usage = field(default_factory=Usage)
    price: Price = field(default_factory=Price)
    cost: Decimal = Decimal(0)
    notes: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {
            "call_uuid": self.call_uuid,
            "tenant_uuid": self.tenant_uuid,
            "started_at": self.started_at,
            "model": self.model,
            "entry_format": self.entry_format,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "key_uuid": self.key_uuid,
            "key_kind": self.key_kind,
            "session_key": (self.session_key or None) and self.session_key[:128],
            "error_category": self.error_category,
            "attempts": self.attempts,
            "first_content_ms": self.first_content_ms,
            "input_tokens": self.usage.input,
            "cache_write_tokens": self.usage.cache_write,
            "cache_hit_tokens": self.usage.cache_hit,
            "output_tokens": self.usage.output,
            "estimated": self.usage.estimated,
            "price": self.price.to_json(),
            "cost": self.cost,
            "notes": self.notes,
        }


class PartitionKeeper:
    """调用记录按月分区：本月和下个月的分区提前建好（启动时、每天各一次）。"""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    @staticmethod
    def _months(now: datetime) -> list[datetime]:
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return [first, nxt, (nxt + timedelta(days=32)).replace(day=1)]

    async def ensure(self, now: datetime | None = None) -> None:
        months = self._months(now or datetime.now(UTC))
        async with self._sm() as session:
            for start, end in itertools.pairwise(months):
                name = f"gw_model_call_{start:%Y%m}"
                await session.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF gw_model_call "
                        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                    )
                )
            await session.commit()

    async def drop_before(self, cutoff: datetime) -> list[str]:
        """保留期之外的分区整块删掉。"""
        dropped: list[str] = []
        async with self._sm() as session:
            rows = await session.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'gw_model_call'"
                )
            )
            for (name,) in rows:
                suffix = name.rsplit("_", 1)[-1]
                if suffix.isdigit() and suffix < f"{cutoff:%Y%m}":
                    await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
                    dropped.append(name)
            await session.commit()
        return dropped


class RecordQueue:
    """内存里的有界队列，后台批量写库。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        batch: int = 200,
        interval_s: float = 1.0,
        max_size: int = 20_000,
    ) -> None:
        self._sm = sessionmaker
        self._batch = batch
        self._interval = interval_s
        self._queue: deque[CallRecord] = deque(maxlen=max_size)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def put(self, record: CallRecord) -> None:
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1
            logger.warning("调用记录攒满了，丢掉最旧的一条（累计丢了 %d 条）", self.dropped)
        self._queue.append(record)
        if len(self._queue) >= self._batch:
            self._wake.set()

    @property
    def pending(self) -> int:
        return len(self._queue)

    async def flush(self) -> int:
        """写一批；写不进去就放回队列，下次再试。返回写了多少条。"""
        written = 0
        while self._queue:
            batch = [self._queue.popleft() for _ in range(min(self._batch, len(self._queue)))]
            try:
                await self._write(batch)
            except Exception:
                logger.warning("写调用记录失败，%d 条留在队列里重试", len(batch), exc_info=True)
                self._queue.extendleft(reversed(batch))
                return written
            written += len(batch)
        return written

    async def _write(self, batch: list[CallRecord]) -> None:
        by_tenant: dict[UUID, list[dict[str, Any]]] = {}
        for record in batch:
            by_tenant.setdefault(record.tenant_uuid, []).append(record.row())
        async with self._sm() as session:
            for tenant, rows in by_tenant.items():
                with TenantScope.enter(tenant):
                    await session.execute(insert(GwModelCall), rows)
            await session.commit()

    def start(self) -> None:
        if self._task is None:
            with TenantScope.isolated():
                self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
            await self.flush()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()


class Metering:
    def __init__(self, queue: RecordQueue) -> None:
        self.queue = queue

    @staticmethod
    def settle(usage: Usage, price: Price) -> Decimal:
        return CostCalculator.cost(usage, price)

    def record(self, record: CallRecord) -> None:
        self.queue.put(record)
