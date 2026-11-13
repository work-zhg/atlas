"""本机 CLI：设备表 device，会话的设备和项目目录

Revision ID: 0045_device
Revises: 0044_thread_workspace_dir
Create Date: 2026-11-13

CLI 智能体多一个运行位置「用户设备」：CLI 跑在使用者自己的电脑上（本机 CLI 方案，
c4-model/agent/device/）。

    device                    一个人配对到平台的一台电脑：令牌只存摘要、默认设备、吊销
    uk_device_default         部分唯一：每人至多一台默认设备（只对 is_default = true 的行）
    thread.device_uuid        本机 CLI 会话跑在哪台电脑
    thread.device_project_dir 电脑上的项目目录（可含 ~，在电脑上展开）

★ 两列都可空：不是本机 CLI 的会话没有；被委派建出的子会话在第一轮才定。
★ 部分唯一索引只在 PostgreSQL 上建（与 0007 的 ux_thread_subagent 相同）；别的数据库靠代码保证。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, dialect, now_default

revision: str = "0045_device"
down_revision: str | Sequence[str] | None = "0044_thread_workspace_dir"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("device_uuid", UUID_COL, nullable=False),
        sa.Column("owner_uuid", UUID_COL, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("os", sa.String(64), nullable=True),
        sa.Column("connector_version", sa.String(32), nullable=True),
        sa.Column("paired_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("last_seen_at", TS_COL, nullable=True),
        sa.Column("revoked_at", TS_COL, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_device"),
        sa.UniqueConstraint("device_uuid", name="uk_device_uuid"),
        sa.UniqueConstraint("token_hash", name="uk_device_token_hash"),
        sa.CheckConstraint("status IN ('active','revoked')", name="device_status_enum"),
    )
    op.create_index("ix_device_owner", "device", ["owner_uuid"])
    if dialect() == "postgresql":
        op.create_index(
            "uk_device_default",
            "device",
            ["owner_uuid"],
            unique=True,
            postgresql_where=sa.text("is_default"),
        )
    op.add_column("thread", sa.Column("device_uuid", UUID_COL, nullable=True))
    op.add_column("thread", sa.Column("device_project_dir", sa.String(1024), nullable=True))


def downgrade() -> None:
    op.drop_column("thread", "device_project_dir")
    op.drop_column("thread", "device_uuid")
    if dialect() == "postgresql":
        op.drop_index("uk_device_default", table_name="device")
    op.drop_index("ix_device_owner", table_name="device")
    op.drop_table("device")
