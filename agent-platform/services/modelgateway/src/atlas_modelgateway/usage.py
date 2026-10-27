"""计量（模型管理方案设计 §9.2、§10）：边转发边读用量，按请求时生效的价目折算成本，异步写库。

    Usage     归一化的用量：input 是不含缓存的输入，cache_read / cache_write 单列，output 含推理
    Meter     喂进上游返回的字节，结束时给出用量。流式（SSE）按事件解析，不缓冲整个响应；
              非流式最多攒 max_meter_bytes，超过就放弃（用量标为未回报）
    Recorder  写用量记录（兼作访问日志）。写库失败先暂存在进程里，维护任务补写，并告警 ——
              记账不能拖慢调用，也不能静默丢失

各协议的用量在哪：
    Anthropic   message_start.message.usage（输入、缓存）+ message_delta.usage（累计输出）；
                非流式在 usage
    OpenAI      usage（流式时是最后一块，要请求里带 stream_options.include_usage —— 网关会补上）；
                缓存读在 prompt_tokens_details.cached_tokens（DeepSeek 是 prompt_cache_hit_tokens）
    嵌入        usage.prompt_tokens / total_tokens
    重排        usage.total_tokens，或 meta.billed_units.input_tokens（Cohere）
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db.models import GwModelCall, GwModelPrice

logger = logging.getLogger(__name__)

__all__ = ["Meter", "Recorder", "Usage", "cost_of"]

_MILLION = Decimal(1_000_000)


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    reported: bool = False

    @property
    def total_input(self) -> int:
        """分段价按它选：输入的全部（含缓存读写）。"""
        return (
            (self.input_tokens or 0)
            + (self.cache_read_tokens or 0)
            + (self.cache_write_tokens or 0)
        )


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _apply_anthropic(usage: Usage, raw: dict[str, Any]) -> None:
    """后到的覆盖先到的：message_delta 的 output_tokens 是累计值。"""
    for key, attr in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cache_read_tokens"),
        ("cache_creation_input_tokens", "cache_write_tokens"),
    ):
        value = _int(raw.get(key))
        if value is not None:
            setattr(usage, attr, value)
            usage.reported = True


def _apply_openai(usage: Usage, raw: dict[str, Any]) -> None:
    prompt = _int(raw.get("prompt_tokens"))
    details = raw.get("prompt_tokens_details") or {}
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is None:
        cached = _int(raw.get("prompt_cache_hit_tokens"))
    miss = _int(raw.get("prompt_cache_miss_tokens"))
    if prompt is not None or miss is not None:
        usage.cache_read_tokens = cached or 0
        usage.input_tokens = miss if miss is not None else max((prompt or 0) - (cached or 0), 0)
        usage.reported = True
    completion = _int(raw.get("completion_tokens"))
    if completion is not None:
        usage.output_tokens = completion
        usage.reported = True
    out_details = raw.get("completion_tokens_details") or {}
    if isinstance(out_details, dict) and _int(out_details.get("reasoning_tokens")) is not None:
        usage.reasoning_tokens = _int(out_details.get("reasoning_tokens"))


def _apply_tokens_only(usage: Usage, raw: dict[str, Any]) -> None:
    """嵌入、重排：只有输入。"""
    tokens = _int(raw.get("prompt_tokens"))
    if tokens is None:
        tokens = _int(raw.get("total_tokens"))
    if tokens is None:
        billed = raw.get("billed_units") or {}
        tokens = _int(billed.get("input_tokens")) if isinstance(billed, dict) else None
    if tokens is not None:
        usage.input_tokens, usage.output_tokens, usage.reported = tokens, 0, True


@dataclass
class Meter:
    """endpoint：messages | chat | embeddings | rerank（另两种不计量）。"""

    endpoint: str
    limit: int
    usage: Usage = field(default_factory=Usage)
    _sse: bool = False
    _buffer: bytearray = field(default_factory=bytearray)
    _overflow: bool = False

    def start(self, content_type: str) -> None:
        self._sse = content_type.partition(";")[0].strip().lower() == "text/event-stream"

    def feed(self, chunk: bytes) -> None:
        if self._overflow:
            return
        self._buffer += chunk
        if self._sse:
            self._drain_events()
        elif len(self._buffer) > self.limit:
            logger.info("响应超过 %d 字节，不再读取用量", self.limit)
            self._overflow = True
            self._buffer.clear()

    def result(self) -> Usage:
        if self._sse:
            self._buffer += b"\n\n"
            self._drain_events()
        elif not self._overflow and self._buffer:
            try:
                body = json.loads(bytes(self._buffer))
            except ValueError:
                body = None
            if isinstance(body, dict):
                self._take(body)
        self._buffer.clear()
        return self.usage

    def _drain_events(self) -> None:
        data = bytes(self._buffer).replace(b"\r\n", b"\n")
        *events, rest = data.split(b"\n\n")
        self._buffer = bytearray(rest)
        if len(self._buffer) > self.limit:
            self._buffer.clear()
        for event in events:
            payload = b"\n".join(
                line[5:].lstrip() for line in event.split(b"\n") if line.startswith(b"data:")
            )
            if not payload or payload == b"[DONE]":
                continue
            try:
                message = json.loads(payload)
            except ValueError:
                continue
            if isinstance(message, dict):
                self._take(message)

    def _take(self, message: dict[str, Any]) -> None:
        if self.endpoint == "messages":
            inner = message.get("message")
            if isinstance(inner, dict) and isinstance(inner.get("usage"), dict):
                _apply_anthropic(self.usage, inner["usage"])
            if isinstance(message.get("usage"), dict):
                _apply_anthropic(self.usage, message["usage"])
        elif self.endpoint == "chat":
            if isinstance(message.get("usage"), dict):
                _apply_openai(self.usage, message["usage"])
        else:
            if isinstance(message.get("usage"), dict):
                _apply_tokens_only(self.usage, message["usage"])
            elif isinstance(message.get("meta"), dict):
                _apply_tokens_only(self.usage, message["meta"])


def _price_for(row: GwModelPrice, total_input: int) -> dict[str, Decimal | None]:
    """这次请求用哪一段的单价：输入超过阈值的段里，阈值最大的那段；都不超过用基础价。"""
    base: dict[str, Decimal | None] = {
        "input": row.input,
        "output": row.output,
        "cache_read": row.cache_read,
        "cache_write": row.cache_write,
    }
    tiers = sorted(
        (t for t in row.tiers or [] if int(t.get("above_input_tokens") or 0) < total_input),
        key=lambda t: int(t.get("above_input_tokens") or 0),
    )
    if tiers:
        top = tiers[-1]
        for key in base:
            if top.get(key) is not None:
                base[key] = Decimal(str(top[key]))
    return base


def cost_of(row: GwModelPrice, usage: Usage) -> Decimal:
    """按一版价目算一次请求的成本。缓存读写没有单独定价的按输入价算。"""
    unit = _price_for(row, usage.total_input)
    input_price = unit["input"] or Decimal(0)
    total = (
        Decimal(usage.input_tokens or 0) * input_price
        + Decimal(usage.output_tokens or 0) * (unit["output"] or Decimal(0))
        + Decimal(usage.cache_read_tokens or 0) * (unit["cache_read"] or input_price)
        + Decimal(usage.cache_write_tokens or 0) * (unit["cache_write"] or input_price)
    ) / _MILLION
    if row.per_request is not None:
        total += row.per_request
    return total.quantize(Decimal("0.00000001"))


async def price_at(session: AsyncSession, model_uuid: UUID, at: datetime) -> GwModelPrice | None:
    return await session.scalar(
        select(GwModelPrice)
        .where(GwModelPrice.model_uuid == model_uuid, GwModelPrice.effective_from <= at)
        .order_by(GwModelPrice.effective_from.desc())
        .limit(1)
    )


@dataclass
class _Pending:
    call: dict[str, Any]
    model_uuid: UUID | None
    usage: dict[str, Any] | None
    at: datetime


class Recorder:
    """用量记录（兼作访问日志）。★ 写失败不能让调用失败：暂存、告警、由维护任务补写。"""

    #: 进程里最多暂存这么多条；再多就丢最早的并告警（库长时间不可用）
    MAX_PENDING = 20_000

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self.pending: list[_Pending] = []

    async def record(
        self,
        call: dict[str, Any],
        *,
        model_uuid: UUID | None,
        usage: Usage | None,
        at: datetime,
    ) -> None:
        item = _Pending(call, model_uuid, asdict(usage) if usage is not None else None, at)
        try:
            async with self.sessionmaker() as session:
                await self._write(session, item)
                await session.commit()
        except Exception:
            logger.warning(
                "写用量记录失败，先暂存（%d 条待补写）", len(self.pending) + 1, exc_info=True
            )
            self.pending.append(item)
            if len(self.pending) > self.MAX_PENDING:
                dropped = len(self.pending) - self.MAX_PENDING
                del self.pending[:dropped]
                logger.error("暂存的用量记录太多，丢掉了最早的 %d 条", dropped)

    async def flush(self) -> int:
        """补写暂存的用量记录。返回写进去几条。"""
        if not self.pending:
            return 0
        batch, self.pending = self.pending, []
        try:
            async with self.sessionmaker() as session:
                for item in batch:
                    await self._write(session, item)
                await session.commit()
        except Exception:
            logger.warning("补写用量记录失败，下一轮再试（%d 条）", len(batch), exc_info=True)
            self.pending = batch + self.pending
            return 0
        logger.info("补写了 %d 条用量记录", len(batch))
        return len(batch)

    async def _write(self, session: AsyncSession, item: _Pending) -> None:
        row = GwModelCall(**item.call, created_at=item.at, updated_at=item.at)
        if item.usage is not None:
            usage = Usage(**item.usage)
            row.input_tokens = usage.input_tokens
            row.output_tokens = usage.output_tokens
            row.cache_read_tokens = usage.cache_read_tokens
            row.cache_write_tokens = usage.cache_write_tokens
            row.reasoning_tokens = usage.reasoning_tokens
            row.usage_reported = usage.reported
            if usage.reported and item.model_uuid is not None:
                price = await price_at(session, item.model_uuid, item.at)
                if price is not None:
                    row.cost, row.currency, row.price_uuid = (
                        cost_of(price, usage),
                        price.currency,
                        price.uuid,
                    )
        session.add(row)
