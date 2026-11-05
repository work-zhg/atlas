"""两张表（建在宿主自己的库里，表名带 app_config 前缀，以免和宿主的表重名）和跨方言的列类型。

    app_config            一个 key 一行：类型（value_kind、element_kind）、当前值、版本
    app_config_revision   每个版本一行，只追加；(config_uuid, version) 唯一，是 CAS 之外的第二道防线

★ 库不依赖宿主：UUID、时间、JSON 列在这里各写一份（与本仓各服务同一约定：PG 上 JSONB、UUID 原生，
  MySQL 上 JSON、CHAR(32)；时间进出都是 aware 的 UTC）。
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

__all__ = [
    "JSONType",
    "UTCDateTime",
    "UUIDType",
    "config_table",
    "metadata",
    "revision_table",
    "uuid7",
]


def uuid7() -> uuid.UUID:
    """UUIDv7：48 位毫秒时间戳 + 版本 7 + 74 位随机数（Python 3.13 的 uuid 模块还没有）。"""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)


UUIDType = sa.Uuid(as_uuid=True)
JSONType = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)


class UTCDateTime(TypeDecorator[datetime]):
    """进出这一层的 datetime 一律是 aware 的 UTC；MySQL 上是 DATETIME(6)。"""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name in ("mysql", "mariadb"):
            from sqlalchemy.dialects import mysql

            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("只接受带时区的时间")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name in ("mysql", "mariadb") else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


metadata = sa.MetaData()

config_table = sa.Table(
    "app_config",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column("config_uuid", UUIDType, nullable=False),
    sa.Column("key", sa.String(128), nullable=False),
    sa.Column("value_kind", sa.String(16), nullable=False),
    sa.Column("element_kind", sa.String(16), nullable=True),
    sa.Column("value", JSONType, nullable=True),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("updated_by", sa.String(64), nullable=False),
    sa.Column("created_at", UTCDateTime(), nullable=False),
    sa.Column("updated_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("config_uuid", name="uk_app_config_uuid"),
    sa.UniqueConstraint("key", name="uk_app_config_key"),
)

revision_table = sa.Table(
    "app_config_revision",
    metadata,
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column("revision_uuid", UUIDType, nullable=False),
    sa.Column("config_uuid", UUIDType, nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("value", JSONType, nullable=True),
    sa.Column("note", sa.Text(), nullable=False),
    sa.Column("changed_by", sa.String(64), nullable=False),
    sa.Column("created_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("revision_uuid", name="uk_app_config_revision_uuid"),
    sa.UniqueConstraint("config_uuid", "version", name="uk_app_config_revision_version"),
)
