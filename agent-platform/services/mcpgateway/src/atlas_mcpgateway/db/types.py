"""跨方言的列类型与逻辑主键。

★ 与 atlas_memory/db/types.py 同一套规则，但**不 import 它**：服务之间不共享代码包。
★ 逻辑主键用 UUIDv7（按时间有序，对索引友好；同用户中心 ids.py）。Python 3.13 的 uuid 模块
  还没有 uuid7，按 RFC 9562 自己生成。
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

__all__ = ["JSONType", "UTCDateTime", "UUIDType", "utcnow", "uuid7"]

UUIDType = sa.Uuid(as_uuid=True)
JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """进出这一层的 datetime 一律是 aware 的 UTC。"""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None or value.tzinfo is None:
            return value
        if dialect.name in ("mysql", "mariadb", "sqlite"):
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def utcnow() -> datetime:
    return datetime.now(UTC)


def uuid7() -> uuid.UUID:
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)
