"""会话的上下文状态单独成表 thread_context（改造计划 A3）

Revision ID: 0033_thread_context
Revises: 0032_approval_call_key
Create Date: 2026-11-03

摘要、覆盖边界、压缩次数和自动压缩的熔断计数原先挂在 thread 上（熔断计数在 context_state 这个
JSON 里）。thread 同时放着标题、审批模式、记忆开关、凭据档案、来源应用，这四项只有上下文管理
读写，单独成表、一个会话一行（c4-model/agent/code-native-runtime-context.html）。

★ 摘要是真实数据：存量的摘要、边界、次数、熔断计数原样搬过去，降级时搬回来。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL_NULLABLE, TS_COL, UUID_COL, dialect, now_default

revision: str = "0033_thread_context"
down_revision: str | Sequence[str] | None = "0032_approval_call_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: thread 上那条「摘要与边界同时有或同时没有」的约束（迁移 0005 建的，名字带一层命名约定前缀）
_THREAD_PAIR = "ck_thread_thread_summary_pair"
_PAIR = "(summary IS NULL) = (summary_upto IS NULL)"


def _mysql() -> bool:
    return dialect() in ("mysql", "mariadb")


def upgrade() -> None:
    op.create_table(
        "thread_context",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("thread_uuid", UUID_COL, nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_upto", TS_COL, nullable=True),
        sa.Column("compact_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_thread_context"),
        sa.UniqueConstraint("thread_uuid", name="uk_thread_context_thread"),
        # 命名约定会拼成 ck_thread_context_summary_pair（与模型一致），这里只给短名
        sa.CheckConstraint(_PAIR, name="summary_pair"),
    )
    failures = (
        "COALESCE(CAST(JSON_UNQUOTE(JSON_EXTRACT(context_state, '$.compaction_failures'))"
        " AS SIGNED), 0)"
        if _mysql()
        else "COALESCE((context_state->>'compaction_failures')::int, 0)"
    )
    op.execute(
        "INSERT INTO thread_context (thread_uuid, summary, summary_upto, compact_count, "
        f"failure_count) SELECT thread_uuid, summary, summary_upto, compact_count, {failures} "
        f"FROM thread WHERE summary IS NOT NULL OR compact_count > 0 OR {failures} > 0"
    )
    # 原生 SQL 按真名删：drop_constraint 会再套一层命名约定（见 0010）
    op.execute(f"ALTER TABLE thread DROP CONSTRAINT {_THREAD_PAIR}")
    op.drop_column("thread", "context_state")
    op.drop_column("thread", "summary_upto")
    op.drop_column("thread", "summary")
    op.drop_column("thread", "compact_count")


def downgrade() -> None:
    op.add_column(
        "thread",
        sa.Column("compact_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("thread", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("thread", sa.Column("summary_upto", TS_COL, nullable=True))
    op.add_column("thread", sa.Column("context_state", JSON_COL_NULLABLE, nullable=True))
    if _mysql():
        op.execute(
            "UPDATE thread t JOIN thread_context c ON c.thread_uuid = t.thread_uuid "
            "SET t.summary = c.summary, t.summary_upto = c.summary_upto, "
            "t.compact_count = c.compact_count, "
            "t.context_state = JSON_OBJECT('compaction_failures', c.failure_count)"
        )
    else:
        op.execute(
            "UPDATE thread SET summary = c.summary, summary_upto = c.summary_upto, "
            "compact_count = c.compact_count, "
            "context_state = jsonb_build_object('compaction_failures', c.failure_count) "
            "FROM thread_context c WHERE c.thread_uuid = thread.thread_uuid"
        )
    op.execute(f"ALTER TABLE thread ADD CONSTRAINT {_THREAD_PAIR} CHECK ({_PAIR})")
    op.drop_table("thread_context")
