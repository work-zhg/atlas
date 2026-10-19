"""Keyset 游标分页。

不用 OFFSET：会话与消息都是持续增长且按时间倒序的列表，OFFSET 在深翻页时
既慢又会因并发插入而重复/漏行。游标编码 (时间戳, id) 二元组，
id 用来打破同一毫秒内的并列。
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from uuid import UUID


class InvalidCursor(ValueError):
    pass


def encode_cursor(ts: datetime, id_: UUID) -> str:
    raw = f"{ts.isoformat()}|{id_}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), UUID(id_str)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor(f"游标格式不合法: {cursor!r}") from exc
