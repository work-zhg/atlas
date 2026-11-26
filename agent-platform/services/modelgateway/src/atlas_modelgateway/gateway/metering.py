"""计量（c4-model/modelgateway/code-gateway-metering.html）：花了多少钱，记一条调用记录。

★ 以供应商回答里给的用量为准，网关不自己数 token；最后一段没收到时才估算，并且标出来。
★ 价格挂在模型上（对内统一价），记录里存下当时的单价；缓存写入默认按输入价算，也可单设 cache_write。
★ 调用记录先放进内存队列，后台批量写库，不让调用方多等；瞬时断连放回队列重试，单条脏数据降级逐条
  隔离丢弃，避免毒丸记录卡死整队。
★ 不存请求和回答的内容。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope
from sqlalchemy import insert
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import GwModelCall
from ..shared.kernel import Price, Usage
from ..shared.partitions import PartitionKeeper

__all__ = [
    "CallRecord",
    "CostCalculator",
    "Metering",
    "PartitionKeeper",
    "RecordQueue",
    "UsageEstimator",
]

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)
_SIX = Decimal("0.000001")
#: 估算 token：输出按字符数 / 3
_CHARS_PER_TOKEN = 3
#: 同一批头元素连续非断连失败超过此次数则降级逐条写入，防止毒丸死锁整队
_MAX_BATCH_FAILURES = 3


def _clean_json(value: Any) -> Any:
    """清理 PostgreSQL jsonb / varchar 不接受的 NUL 字节。"""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_clean_json(v) for v in value]
    if isinstance(value, dict):
        return {_clean_json(k): _clean_json(v) for k, v in value.items()}
    return value


def _is_data_error(exc: Exception) -> bool:
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return False
    return isinstance(exc, DataError | IntegrityError | ProgrammingError | ValueError | TypeError)


class CostCalculator:
    @staticmethod
    def cost(usage: Usage, price: Price) -> Decimal:
        if not price.billable:
            return Decimal(0)
        total = (
            Decimal(usage.input) * price.input
            + Decimal(usage.cache_write) * price.effective_cache_write
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
        session = self.session_key.replace("\x00", "")[:128] if self.session_key else None
        return {
            "call_uuid": self.call_uuid,
            "tenant_uuid": self.tenant_uuid,
            "started_at": self.started_at,
            "model": self.model.replace("\x00", "")[:128],
            "entry_format": self.entry_format,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "key_uuid": self.key_uuid,
            "key_kind": self.key_kind,
            "session_key": session or None,
            "error_category": self.error_category,
            "attempts": _clean_json(self.attempts),
            "first_content_ms": self.first_content_ms,
            "input_tokens": self.usage.input,
            "cache_write_tokens": self.usage.cache_write,
            "cache_hit_tokens": self.usage.cache_hit,
            "output_tokens": self.usage.output,
            "estimated": self.usage.estimated,
            "price": self.price.to_json(),
            "cost": self.cost,
            "notes": _clean_json(self.notes),
        }


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
        self._head_uuid: UUID | None = None
        self._head_failures = 0
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
        """写一批；连接瞬断放回队列下次再试，数据错误降级逐条隔离毒丸。返回写了多少条。"""
        written = 0
        while self._queue:
            batch = [self._queue.popleft() for _ in range(min(self._batch, len(self._queue)))]
            head = batch[0].call_uuid
            try:
                await self._write(batch)
                self._head_uuid = None
                self._head_failures = 0
                written += len(batch)
            except Exception as exc:
                if self._head_uuid == head:
                    self._head_failures += 1
                else:
                    self._head_uuid = head
                    self._head_failures = 1
                if _is_data_error(exc) or self._head_failures >= _MAX_BATCH_FAILURES:
                    ok, rest = await self._write_isolated(batch)
                    written += ok
                    if rest:
                        self._queue.extendleft(reversed(rest))
                        return written
                    self._head_uuid = None
                    self._head_failures = 0
                else:
                    logger.warning("写调用记录失败，%d 条留在队列里重试", len(batch), exc_info=True)
                    self._queue.extendleft(reversed(batch))
                    return written
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

    async def _write_isolated(self, batch: list[CallRecord]) -> tuple[int, list[CallRecord]]:
        """批量失败后逐条写入：正常记录照常入库，坏行记错误并丢弃，遇到网络断连则把剩余放回队列。"""
        written = 0
        for idx, record in enumerate(batch):
            try:
                await self._write([record])
                written += 1
            except Exception as exc:
                if _is_data_error(exc) or self._head_failures >= _MAX_BATCH_FAILURES:
                    self.dropped += 1
                    logger.error(
                        "丢弃无法入库的调用记录 call_uuid=%s（累计丢了 %d 条）",
                        record.call_uuid,
                        self.dropped,
                        exc_info=True,
                    )
                else:
                    logger.warning(
                        "逐条写调用记录时数据库不可达，剩余 %d 条留在队列重试",
                        len(batch) - idx,
                        exc_info=True,
                    )
                    return written, batch[idx:]
        return written, []

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
