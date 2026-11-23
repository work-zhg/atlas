"""标准化有序事件流原语（c4-model/infra-platform/code-redis.html）。

面向 Agent SSE 会话流等「持续追加 + 按单调序号续传」的场景：
  1. 序号分配走 ScriptRegistry（EVALSHA），保证多并发生产者与 Redis 易失恢复下原子递增；
  2. 发布走 commands 池 pipeline(transaction=False)，XADD + EXPIRE 单次往返完成；
  3. 订阅回放走 blocking 池（不挤占短命令池），并做两项关键加固：
     · 末尾快跳：先 XREVRANGE COUNT 1 看流尾序号，若 <= after_seq 直接跳到流尾进 XREAD，
       将末尾重连的 O(N) 扫描降为 O(1)；
     · 坏帧容错：单条损坏 payload 记告警跳过，绝不掐断整条 SSE。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .client import RedisClient
from .scripts import ScriptRegistry

__all__ = ["StreamChannel", "StreamRecord"]

logger = logging.getLogger(__name__)

_SCRIPT_NEXT_SEQ = "atlas_redis.stream.next_seq"
_LUA_NEXT_SEQ = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('SET', KEYS[1], ARGV[1])
end
local v = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return v
"""


@dataclass(frozen=True, slots=True)
class StreamRecord:
    entry_id: str
    seq: int
    fields: Mapping[str, Any]


class StreamChannel:
    """基于 Redis Stream 的有序事件流原语。"""

    def __init__(
        self,
        *,
        commands: RedisClient,
        blocking: RedisClient,
        scripts: ScriptRegistry,
        maxlen: int = 10_000,
        ttl_s: int = 86_400,
        seq_ttl_s: int = 30 * 86_400,
        page_size: int = 500,
    ) -> None:
        self._commands = commands
        self._blocking = blocking
        self._scripts = scripts
        self._maxlen = maxlen
        self._ttl_s = ttl_s
        self._seq_ttl_s = seq_ttl_s
        self._page_size = page_size
        self._scripts.register(_SCRIPT_NEXT_SEQ, _LUA_NEXT_SEQ)

    async def next_seq(
        self, seq_key: str, *, floor: int = 0, key_pattern: str = "stream:seq"
    ) -> int:
        """原子分配下一个单调序号（key 不存在时按 DB 水位 floor 初始化）。"""
        val = await self._scripts.run(
            _SCRIPT_NEXT_SEQ,
            keys=(seq_key,),
            args=(str(floor), str(self._seq_ttl_s)),
            key_pattern=key_pattern,
        )
        return int(val)

    async def publish(
        self,
        stream_key: str,
        fields: Mapping[str, Any],
        *,
        maxlen: int | None = None,
        ttl_s: int | None = None,
        key_pattern: str = "stream:publish",
    ) -> str:
        """单次往返完成 XADD（近似裁剪）与 EXPIRE 续期。"""
        async with self._commands.pipeline(transaction=False, key_pattern=key_pattern) as pipe:
            pipe.xadd(
                stream_key,
                fields,
                maxlen=self._maxlen if maxlen is None else maxlen,
                approximate=True,
            )
            pipe.expire(stream_key, self._ttl_s if ttl_s is None else ttl_s)
            res = await pipe.execute()
        entry_id = res[0]
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    async def tail[T](
        self,
        stream_key: str,
        *,
        after_seq: int,
        block_ms: int = 15_000,
        seq_field: str = "thread_seq",
        decode: Callable[[Mapping[str, Any]], T] | None = None,
        key_pattern: str = "stream:tail",
    ) -> AsyncIterator[T | StreamRecord | None]:
        """按序号持续消费事件流；心跳超时产出 None，永不因终态事件自行结束。"""
        last_id = "0-0"

        # ★ 末尾快跳：大多数重连时客户端游标已在流尾，先看最后一条避免扫 10,000 条再全丢
        latest = await self._commands.xrevrange(
            stream_key, max="+", min="-", count=1, key_pattern=f"{key_pattern}:tip"
        )
        if not latest:
            need_replay = False
        else:
            tip_id, tip_fields = latest[0]
            tip_seq = _read_seq(tip_fields, seq_field)
            if tip_seq <= after_seq:
                last_id = _decode_id(tip_id)
                need_replay = False
            else:
                need_replay = True

        if need_replay:
            start = "-"
            while True:
                page = await self._commands.xrange(
                    stream_key,
                    min=start,
                    max="+",
                    count=self._page_size,
                    key_pattern=f"{key_pattern}:replay",
                )
                for raw_id, fields in page:
                    last_id = _decode_id(raw_id)
                    item = _filter_and_decode(
                        last_id, fields, after_seq=after_seq, seq_field=seq_field, decode=decode
                    )
                    if item is not None:
                        yield item
                if len(page) < self._page_size:
                    break
                start = f"({last_id}"

        while True:
            resp = await self._blocking.xread(
                {stream_key: last_id},
                block=block_ms,
                count=100,
                key_pattern=f"{key_pattern}:block",
            )
            if not resp:
                yield None
                continue
            for _key, entries in resp:
                for raw_id, fields in entries:
                    last_id = _decode_id(raw_id)
                    item = _filter_and_decode(
                        last_id, fields, after_seq=after_seq, seq_field=seq_field, decode=decode
                    )
                    if item is not None:
                        yield item


def _decode_id(raw: Any) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _read_seq(fields: Mapping[str, Any], seq_field: str) -> int:
    raw = fields.get(seq_field)
    if raw is None and isinstance(seq_field, str):
        raw = fields.get(seq_field.encode())
    if raw is None:
        return 0
    try:
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return 0


def _filter_and_decode[T](
    entry_id: str,
    fields: Mapping[str, Any],
    *,
    after_seq: int,
    seq_field: str,
    decode: Callable[[Mapping[str, Any]], T] | None,
) -> T | StreamRecord | None:
    seq = _read_seq(fields, seq_field)
    if seq <= after_seq:
        return None
    if decode is None:
        return StreamRecord(entry_id=entry_id, seq=seq, fields=fields)
    try:
        return decode(fields)
    except Exception:
        logger.warning(
            "StreamChannel 跳过损坏消息：entry_id=%s seq=%d",
            entry_id,
            seq,
            exc_info=True,
        )
        return None
