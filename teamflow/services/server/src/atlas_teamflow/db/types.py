"""列类型。★ 与用户中心同一套规则，但不 import 它（服务间不共享代码包）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

__all__ = ["JSONType", "UTCDateTime", "UUIDType", "utcnow"]

UUIDType = sa.Uuid(as_uuid=True)
JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """进出这一层的 datetime 一律是 aware 的 UTC。"""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def utcnow() -> datetime:
    return datetime.now(UTC)
