"""迁移脚本用的跨方言构件。

与 `db/types.py` 的分工：那边给 ORM 模型用，这边给 Alembic 用。
两边必须给出**同一套物理类型**，否则 `alembic upgrade` 建出来的表
和 `compare_type` 认为的模型不一致，每次 autogenerate 都会生成一堆假迁移。

放在包里而不是 migrations/ 目录下：env.py 已经 import atlas_server，
包一定是可导入的；放 migrations/ 则要依赖 alembic 的 sys.path 注入，
换个调用方式就找不到。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from .types import UTCDateTime

#: 与模型里的 UUIDType 同一物理类型：PG 原生 uuid，MySQL CHAR(32)。
UUID_COL = sa.Uuid(as_uuid=True)

#: 与模型里的 JSONType 同一物理类型。
JSON_COL = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
JSON_COL_NULLABLE = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)

#: 时间列。MySQL 上是 DATETIME(6) —— 精度不能丢，否则消息排序会并列。
TS_COL = UTCDateTime


def dialect() -> str:
    return op.get_bind().dialect.name


def now_default() -> sa.TextClause:
    """建表时的「当前时间」默认值。

    PG 用 now()；MySQL 必须写成 CURRENT_TIMESTAMP(6) —— 不带精度的话
    默认值是秒级，而列本身是 DATETIME(6)，MySQL 会直接拒绝（1067）。
    """
    return sa.text("CURRENT_TIMESTAMP(6)") if dialect() in ("mysql", "mariadb") else sa.text("now()")


def uuid_pk_default() -> sa.TextClause | None:
    """主键的服务端默认值。

    PG 有 gen_random_uuid()；MySQL 没有对应物（UUID() 给的是带连字符的
    36 位，和 CHAR(32) 对不上）。MySQL 上返回 None —— 主键由 ORM 在
    Python 侧生成（见 models._uuid_pk）。
    <p>
    于是「直接写 SQL 插入」在 MySQL 上必须自带 id。迁移里的种子数据
    本来就显式给 id，不受影响。
    """
    return None if dialect() in ("mysql", "mariadb") else sa.text("gen_random_uuid()")


def text_default(value: str) -> str | None:
    """TEXT / JSON 列的字面量默认值。

    MySQL 不接受（1101），返回 None 由应用侧兜底；PG 原样给。
    这不是「MySQL 少了个默认值」——模型里对应的列都配了 Python 侧 default，
    两边行为一致。
    """
    return None if dialect() in ("mysql", "mariadb") else value
