"""message.created_at 改用 clock_timestamp()

Revision ID: 0003_message_clock_timestamp
Revises: 0002_builtin_agent
Create Date: 2026-08-19

★ 要解决的问题：message.created_at 必须**逐行不同**。
  消息列表按 (created_at DESC, id DESC) 排序，并列时退化为按随机 UUID 排 ——
  聊天记录顺序变成任意的。分页本身仍然正确（无重复、无遗漏），
  坏掉的是**展示顺序**，这对聊天记录是硬伤。

  PG 上的病根是 now() 返回事务开始时间；原方案换成 clock_timestamp()。
  但 clock_timestamp() 是 PG 独有的，而 MySQL 有它自己的版本：
  CURRENT_TIMESTAMP 虽是每语句取值，DATETIME 的默认精度却只到**秒**，
  同一秒内照样并列。

  所以现在两边都不用服务端默认值，改由应用侧逐行生成（见 models.Message）。
  服务端默认值在这里一并去掉 —— 留着它只会在有人绕过 ORM 直接 INSERT 时
  悄悄给出一个会并列的值。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from atlas_server.db.migration_types import TS_COL, now_default

revision: str = "0003_message_clock_timestamp"
down_revision: str | None = "0002_builtin_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 去掉服务端默认值：时间改由应用侧逐行生成，两个方言上都能保证互不相同
    op.alter_column(
        "message",
        "created_at",
        server_default=None,
        existing_type=TS_COL,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "message",
        "created_at",
        server_default=now_default(),
        existing_type=TS_COL,
        existing_nullable=False,
    )
