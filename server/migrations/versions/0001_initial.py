"""initial schema + app_user / model_catalog seed

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-19

注意：
  · 本项目没有接 checkpointer，历史每轮从 message 表重建，没有别的表。
  · 列类型走 db/migration_types —— **迁移要能在 PG 与 MySQL 上都跑通**，
    所以这里不出现 postgresql.* 与 gen_random_uuid() 这类厂商专有物。
  · agent 与 agent_version 互相引用，FK 分两步建（先建表，后加约束）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
import uuid

from atlas_server.db.migration_types import (
    JSON_COL,
    TS_COL,
    UUID_COL,
    dialect,
    now_default,
    text_default,
    uuid_pk_default,
)

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = UUID_COL

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"

# ★ 2026-08-19 实测值，非推测（见 docs/backend-design.md §3）：
#   context_window / max_output_tokens 来自 Anthropic 官方模型表
#   supports_temperature：opus-5 / sonnet-5 / fable-5 返回
#     "`temperature` is deprecated for this model." (HTTP 400)，仅 haiku-4-5 接受
#   min_cacheable_tokens 非单调：opus-5=512 < sonnet-5=1024 < haiku-4-5=4096
_MODEL_SEED: tuple[dict[str, object], ...] = (
    {
        "model": "claude-opus-5",
        "provider": "anthropic",
        "display_name": "Claude Opus 5",
        "context_window": 1_000_000,
        "max_output_tokens": 128_000,
        "supports_thinking": True,
        "supports_adaptive_thinking": True,
        "supports_effort": True,
        "supports_cache": True,
        "supports_temperature": False,
        "min_cacheable_tokens": 512,
    },
    {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "display_name": "Claude Sonnet 5",
        "context_window": 1_000_000,
        "max_output_tokens": 128_000,
        "supports_thinking": True,
        "supports_adaptive_thinking": True,
        "supports_effort": True,
        "supports_cache": True,
        "supports_temperature": False,
        "min_cacheable_tokens": 1024,
    },
    {
        "model": "claude-fable-5",
        "provider": "anthropic",
        "display_name": "Claude Fable 5",
        "context_window": 1_000_000,
        "max_output_tokens": 128_000,
        # thinking 恒开，且无法关闭（传 disabled 会 400）
        "supports_thinking": True,
        "supports_adaptive_thinking": True,
        "supports_effort": True,
        "supports_cache": True,
        "supports_temperature": False,
        "min_cacheable_tokens": 512,
    },
    {
        "model": "claude-haiku-4-5",
        "provider": "anthropic",
        "display_name": "Claude Haiku 4.5",
        "context_window": 200_000,
        "max_output_tokens": 64_000,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "supports_effort": False,
        "supports_cache": True,
        "supports_temperature": True,  # ★ 唯一接受 temperature 的模型
        "min_cacheable_tokens": 4096,
    },
)


def upgrade() -> None:
    # ---------------- app_user ----------------
    op.create_table(
        "app_user",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column("name", sa.Text(), nullable=False),
        # String 而非 Text：它进了唯一索引，而 MySQL 不能给无长度的 TEXT 建索引
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
    )
    op.create_index(
        "ux_app_user_external_id",
        "app_user",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    # ---------------- model_catalog ----------------
    op.create_table(
        "model_catalog",
        sa.Column("model", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("supports_thinking", sa.Boolean(), nullable=False),
        sa.Column("supports_adaptive_thinking", sa.Boolean(), nullable=False),
        sa.Column("supports_effort", sa.Boolean(), nullable=False),
        sa.Column("supports_cache", sa.Boolean(), nullable=False),
        sa.Column("supports_temperature", sa.Boolean(), nullable=False),
        sa.Column("min_cacheable_tokens", sa.Integer(), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("context_window > 0", name="ck_model_catalog_context_window_positive"),
        sa.CheckConstraint("max_output_tokens > 0", name="ck_model_catalog_max_output_positive"),
    )

    # ---------------- agent (current_version_id 的 FK 稍后加) ----------------
    op.create_table(
        "agent",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=text_default("")),
        sa.Column("avatar_key", sa.String(32), nullable=False, server_default="general"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("current_version_id", _UUID, nullable=True),
        sa.Column("created_by", _UUID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
        sa.Column("updated_at", TS_COL, server_default=now_default(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','enabled','archived')", name="ck_agent_status_enum"
        ),
    )

    op.create_table(
        "agent_version",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column(
            "agent_id", _UUID, sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", JSON_COL, nullable=False),
        sa.Column("created_by", _UUID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_version"),
    )
    op.create_foreign_key(
        "fk_agent_current_version", "agent", "agent_version", ["current_version_id"], ["id"]
    )

    # ---------------- thread ----------------
    op.create_table(
        "thread",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column("agent_id", _UUID, sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=text_default("")),
        sa.Column("title_source", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "latest_state", JSON_COL, nullable=False, server_default=text_default("{}")
        ),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("compact_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", _UUID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
        sa.Column("updated_at", TS_COL, server_default=now_default(), nullable=False),
        sa.CheckConstraint(
            "title_source IN ('pending','generated','fallback','manual')",
            name="ck_thread_title_source_enum",
        ),
    )
    op.execute("CREATE INDEX ix_thread_list ON thread (status, updated_at DESC)")

    # ---------------- message ----------------
    op.create_table(
        "message",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column(
            "thread_id", _UUID, sa.ForeignKey("thread.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("run_id", _UUID, nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", JSON_COL, nullable=False),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_message_role_enum"),
    )
    op.create_index("ix_message_thread", "message", ["thread_id", "created_at"])

    # ---------------- run ----------------
    op.create_table(
        "run",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column(
            "thread_id", _UUID, sa.ForeignKey("thread.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "agent_version_id", _UUID, sa.ForeignKey("agent_version.id"), nullable=False
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("error_kind", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("thinking_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", TS_COL, nullable=True),
        sa.Column("finished_at", TS_COL, nullable=True),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled',"
            "'interrupted','awaiting_approval')",
            name="ck_run_status_enum",
        ),
    )
    op.execute("CREATE INDEX ix_run_thread ON run (thread_id, created_at DESC)")
    if dialect() in ("mysql", "mariadb"):
        # MySQL 没有分部索引。这个索引是**查询优化不是约束** ——
        # 去掉 WHERE 只是让索引大一些（多存了终态 run），语义完全不变。
        op.execute("CREATE INDEX ix_run_active ON run (status)")
    else:
        op.execute(
            "CREATE INDEX ix_run_active ON run (status) "
            "WHERE status IN ('queued','running','awaiting_approval')"
        )

    # ---------------- run_event ----------------
    op.create_table(
        "run_event",
        sa.Column("run_id", _UUID, sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", TS_COL, nullable=False),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("depth", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("data", JSON_COL, nullable=False),
        sa.PrimaryKeyConstraint("run_id", "seq", name="pk_run_event"),
    )

    # ---------------- run_file ----------------
    op.create_table(
        "run_file",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column(
            "thread_id", _UUID, sa.ForeignKey("thread.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("run_id", _UUID, sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        # String 而非 Text：它是 ix_run_file_thread 的一部分，
        # 而 MySQL 不能给无长度的 TEXT 建索引
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_run_file_thread ON run_file (thread_id, path, created_at DESC)"
    )

    # ---------------- approval ----------------
    op.create_table(
        "approval",
        sa.Column("id", _UUID, primary_key=True, server_default=uuid_pk_default()),
        sa.Column("run_id", _UUID, sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("args", JSON_COL, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", _UUID, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("decided_at", TS_COL, nullable=True),
        sa.Column("created_at", TS_COL, server_default=now_default(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="ck_approval_status_enum",
        ),
    )

    # ---------------- seed ----------------
    # 用 ORM 层的类型绑定而不是手写 SQL：CAST(... AS uuid) 与 ON CONFLICT
    # 都是 PG 专有写法，而 UUID 在两个方言上的存储形式本来就不同
    # （PG 原生 uuid，MySQL CHAR(32)）—— 交给 UUID_COL 去处理。
    app_user = sa.table("app_user", sa.column("id", UUID_COL), sa.column("name", sa.Text))
    bind = op.get_bind()
    exists = bind.execute(
        sa.select(sa.literal(1)).select_from(app_user).where(
            app_user.c.id == uuid.UUID(DEFAULT_USER_ID)
        )
    ).first()
    if exists is None:
        op.bulk_insert(app_user, [{"id": uuid.UUID(DEFAULT_USER_ID), "name": "default"}])

    model_catalog = sa.table(
        "model_catalog",
        sa.column("model", sa.String),
        sa.column("provider", sa.String),
        sa.column("display_name", sa.Text),
        sa.column("context_window", sa.Integer),
        sa.column("max_output_tokens", sa.Integer),
        sa.column("supports_thinking", sa.Boolean),
        sa.column("supports_adaptive_thinking", sa.Boolean),
        sa.column("supports_effort", sa.Boolean),
        sa.column("supports_cache", sa.Boolean),
        sa.column("supports_temperature", sa.Boolean),
        sa.column("min_cacheable_tokens", sa.Integer),
    )
    op.bulk_insert(model_catalog, list(_MODEL_SEED))


def downgrade() -> None:
    for table in (
        "approval",
        "run_file",
        "run_event",
        "run",
        "message",
        "thread",
    ):
        op.drop_table(table)
    op.drop_constraint("fk_agent_current_version", "agent", type_="foreignkey")
    op.drop_table("agent_version")
    op.drop_table("agent")
    op.drop_table("model_catalog")
    op.drop_index("ux_app_user_external_id", table_name="app_user")
    op.drop_table("app_user")
