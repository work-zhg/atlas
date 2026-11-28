"""应用工作空间 app_workspace；会话的文件主体 thread.owner_uuid / owner_kind / app_workspace_uuid

Revision ID: 0050_app_workspace
Revises: 0049_tenant_instruction
Create Date: 2026-11-28

sandbox/design-workspace-subject.html、code-workspace-app.html。会话的文件归谁由权限类型定：以人为权限
是创建人，以数字员工为权限是数字员工；建会话时定、不改。存量会话都按以人为权限处理（回填创建人）——
以前的会话文件本来就在创建人前缀下，改了权限类型只影响新会话。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0050_app_workspace"
down_revision: str | Sequence[str] | None = "0049_tenant_instruction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_workspace",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_uuid", UUID_COL, nullable=False),
        sa.Column("space_uuid", UUID_COL, nullable=False),
        sa.Column("app_code", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("prefix", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by_uuid", UUID_COL, nullable=False),
        sa.Column("released_at", TS_COL, nullable=True),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_app_workspace"),
        sa.UniqueConstraint("space_uuid", name="uk_app_workspace_space"),
        sa.CheckConstraint(
            "status IN ('active','released')", name=op.f("ck_app_workspace_status_enum")
        ),
    )
    op.create_index("ix_app_workspace_tenant_uuid", "app_workspace", ["tenant_uuid"])

    op.add_column("thread", sa.Column("owner_uuid", UUID_COL, nullable=True))
    op.execute(sa.text("UPDATE thread SET owner_uuid = created_by_uuid"))
    op.alter_column("thread", "owner_uuid", existing_type=UUID_COL, nullable=False)
    op.add_column(
        "thread", sa.Column("owner_kind", sa.String(8), nullable=False, server_default="user")
    )
    op.create_check_constraint(
        op.f("ck_thread_owner_kind_enum"), "thread", "owner_kind IN ('user','agent')"
    )
    op.add_column("thread", sa.Column("app_workspace_uuid", UUID_COL, nullable=True))
    # 工作空间里改名、删目录前按文件主体查「哪些会话在用它」
    op.drop_index("ix_thread_workspace_dir", table_name="thread")
    op.create_index("ix_thread_workspace_dir", "thread", ["owner_uuid", "workspace_dir"])


def downgrade() -> None:
    op.drop_index("ix_thread_workspace_dir", table_name="thread")
    op.create_index("ix_thread_workspace_dir", "thread", ["created_by_uuid", "workspace_dir"])
    op.drop_column("thread", "app_workspace_uuid")
    op.drop_constraint(op.f("ck_thread_owner_kind_enum"), "thread", type_="check")
    op.drop_column("thread", "owner_kind")
    op.drop_column("thread", "owner_uuid")
    op.drop_index("ix_app_workspace_tenant_uuid", table_name="app_workspace")
    op.drop_table("app_workspace")
