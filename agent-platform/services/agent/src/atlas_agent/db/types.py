"""跨方言的列类型。Postgres 与 MySQL 共用一套模型定义。

为什么需要这一层：模型里直接写 `postgresql.JSONB` / `postgresql.UUID`，
整个 schema 就和一个厂商焊死了。这些类型在别的库上都有等价物，
差别只在名字和存储形式 —— 那属于方言细节，不该出现在业务表定义里。

三件事分别处理：

  UUID   SQLAlchemy 2.0 的 `sa.Uuid` 本身就是方言感知的：
         PG 上是原生 uuid，MySQL 上落成 CHAR(32)。直接用它即可。
         但**主键默认值要挪到 Python 侧** —— `gen_random_uuid()` 是 PG 内置函数，
         MySQL 没有；而 MySQL 8 的表达式默认值要求 `DEFAULT (UUID())` 且
         格式带连字符，和 CHAR(32) 对不上。在 Python 里生成还有个额外好处：
         insert 之前就知道 id，不必等 flush 回读。

  JSON   PG 上保留 JSONB（有 GIN 索引与包含查询），其余方言用标准 JSON。
         `with_variant` 就是为这种情况设计的。

  时间   **这个最容易出事。** 业务代码写的是 `datetime.now(UTC)`（aware），
         PG 的 timestamptz 能原样存取；MySQL 的 DATETIME 没有时区，
         读回来是 naive，于是「存进去是 aware、读出来是 naive」，
         接下来任何一次 `a < b` 都会 TypeError，而且报错点离病根很远。
         所以这里用 TypeDecorator 在边界上归一：写入转 UTC 去掉 tzinfo，
         读出补回 UTC —— 让 MySQL 在应用看来和 PG 行为一致。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """带时区语义的时间列，在没有时区类型的方言上也成立。

    不变式：**进出这一层的 datetime 一律是 aware 的 UTC**。
    库里存什么形式是方言的事，业务代码不需要知道。
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        """MySQL 上必须显式要微秒。

        MySQL 的 DATETIME **默认精度是秒**，同一秒内写入的多行时间完全相同。
        message 列表按 (created_at DESC, id DESC) 排序，并列时退化为按随机 UUID 排 ——
        聊天记录顺序变成任意的。这正是 PG 那边迁移 0003 要解决的问题，
        在 MySQL 上只会更容易触发（PG 至少是每事务一个值，MySQL 是每秒一个值）。
        """
        if dialect.name in ("mysql", "mariadb"):
            from sqlalchemy.dialects import mysql

            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # naive 一律按 UTC 解释。猜本地时区只会让同一份代码在不同机器上行为不同
            return value
        return value.astimezone(UTC).replace(tzinfo=None) if _naive_dialect(dialect) else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _naive_dialect(dialect: Dialect) -> bool:
    """该方言的时间类型是否不带时区。"""
    return dialect.name in ("mysql", "mariadb", "sqlite")


#: 主键与外键用的 UUID。PG 原生 uuid，MySQL CHAR(32)。
UUIDType = sa.Uuid(as_uuid=True)

#: 结构化列。PG 用 JSONB，其余用标准 JSON。
JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

#: 可空结构化列：None 要存成 SQL NULL 而不是 JSON 的 null —— 两者语义不同。
JSONTypeNullable = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)


def json_default(dialect_agnostic: Any = dict) -> Any:
    """JSON 列的默认值走 Python 侧。

    MySQL 8 不接受 JSON 列的字面量默认值（1101），改用表达式默认值又要
    `DEFAULT ('{}')` 这种方言特有写法。在 Python 里给默认值两边都成立，
    且省掉一次「这个默认值到底是谁给的」的排查。
    """
    return dialect_agnostic
