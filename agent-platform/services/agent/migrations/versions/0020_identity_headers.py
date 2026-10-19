"""内网身份传递：去服务账号与登录会话，会话记来源应用，run 记按谁判（doc/identity-propagation-design.html）

Revision ID: 0020_identity_headers
Revises: 0019_drop_manage_all_ops
Create Date: 2026-10-19

- thread.source_app：建会话的业务应用（取代 owner_kind）。存量里服务账号的会话按账号名回填
  （teamflow → teamflow，atlas-bench → bench，其它 = 账号名）；
- run.auth_uid：这一轮按谁的权限判，存量回填为发起人；
- 删 thread.owner_kind、service_account、auth_session（登录在用户中心 + 网关）。

★ 服务账号建的会话 / 智能体，created_by 仍是原服务账号 id（已无对应的人）。会话照常可以经
  来源应用访问（同一应用来访时不要求 uid = 所有者）；要让它们出现在发起人的会话列表里，
  用 `python -m atlas_agent reassign-threads <TeamFlow 导出的映射>` 改写（§14）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL

revision: str = "0020_identity_headers"
down_revision: str | Sequence[str] | None = "0019_drop_manage_all_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("source_app", sa.String(32), nullable=True))
    conn = op.get_bind()
    # 子会话的 created_by 继承自父会话，按 created_by 回填就同时覆盖了子会话
    for sa_id, name in conn.execute(sa.text("SELECT id, name FROM service_account")).all():
        lowered = str(name).lower()
        app = (
            "teamflow" if "teamflow" in lowered else "bench" if "bench" in lowered else lowered
        )[:32]
        conn.execute(
            sa.text(
                "UPDATE thread SET source_app = :app "
                "WHERE owner_kind = 'service' AND created_by = :id"
            ),
            {"app": app, "id": sa_id},
        )
    op.drop_column("thread", "owner_kind")

    op.add_column("run", sa.Column("auth_uid", sa.Uuid(), nullable=True))
    op.execute("UPDATE run SET auth_uid = created_by")

    op.drop_table("service_account")
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")


def downgrade() -> None:
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
    op.drop_column("run", "auth_uid")
    op.add_column(
        "thread", sa.Column("owner_kind", sa.String(8), server_default="user", nullable=False)
    )
    # 服务账号已无从还原：有来源应用的会话一律记为 service（所有者 id 未变）
    op.execute("UPDATE thread SET owner_kind = 'service' WHERE source_app IS NOT NULL")
    op.drop_column("thread", "source_app")
