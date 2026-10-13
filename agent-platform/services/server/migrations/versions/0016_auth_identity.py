"""身份与鉴权：接入用户中心（doc/auth-permission-design.html）

Revision ID: 0016_auth_identity
Revises: 0015_thread_git_profile
Create Date: 2026-10-10

- app_user：用户中心用户的投影（id = 用户中心用户 id），补资料与状态字段；
- auth_session：浏览器登录会话（只存令牌哈希）；
- service_account：后端服务的主体（只存密钥哈希）；
- thread.owner_kind / thread.memory：所有者类型与用户记忆开关；
- run.created_by：这一轮的发起人（存量回填为会话所有者）；
- 会话列表按所有者查询的索引。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import JSON_COL

revision: str = "0016_auth_identity"
down_revision: str | Sequence[str] | None = "0015_thread_git_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for col in (
        sa.Column("account", sa.String(128), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("dept_id", sa.String(64), nullable=True),
        sa.Column("dept_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("app_user", col)

    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_session_token_hash")),
    )
    op.create_index(op.f("ix_auth_session_user_id"), "auth_session", ["user_id"])

    op.create_table(
        "service_account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("operations", JSON_COL, nullable=False),
        sa.Column("agent_scope", sa.String(16), server_default="all_enabled", nullable=False),
        sa.Column("agent_ids", JSON_COL, nullable=False),
        sa.Column("allow_acting", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("memory_default", sa.String(8), server_default="off", nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "agent_scope IN ('all_enabled','list')", name="ck_service_account_agent_scope_enum"
        ),
        sa.CheckConstraint(
            "memory_default IN ('on','off')", name="ck_service_account_memory_default_enum"
        ),
        sa.CheckConstraint(
            "status IN ('active','disabled')", name="ck_service_account_status_enum"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_account")),
        sa.UniqueConstraint("name", name=op.f("uq_service_account_name")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_service_account_key_hash")),
    )

    op.add_column(
        "thread", sa.Column("owner_kind", sa.String(8), server_default="user", nullable=False)
    )
    op.add_column("thread", sa.Column("memory", sa.String(8), server_default="on", nullable=False))
    op.create_index("ix_thread_owner_recent", "thread", ["created_by", "updated_at", "id"])

    op.add_column("run", sa.Column("created_by", sa.Uuid(), nullable=True))
    # 存量 run 的发起人 = 会话所有者（子会话的 created_by 继承自父会话，同样成立）
    op.execute(
        "UPDATE run SET created_by = (SELECT thread.created_by FROM thread "
        "WHERE thread.id = run.thread_id)"
    )


def downgrade() -> None:
    op.drop_column("run", "created_by")
    op.drop_index("ix_thread_owner_recent", table_name="thread")
    op.drop_column("thread", "memory")
    op.drop_column("thread", "owner_kind")
    op.drop_table("service_account")
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")
    for name in (
        "updated_at", "last_seen_at", "status", "dept_name", "dept_id", "email", "account"
    ):
        op.drop_column("app_user", name)
