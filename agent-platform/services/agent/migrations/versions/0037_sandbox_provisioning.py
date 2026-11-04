"""沙箱 · 通用供给：sandbox_account、user_sandbox、sandbox_binding（改造计划 B5）

Revision ID: 0037_sandbox_provisioning
Revises: 0036_thread_credential_profiles
Create Date: 2026-11-04

c4-model/agent/sandbox/code-sandbox-provisioning.html：按人管理沙箱。

    sandbox_account   一人一行，只有版本号：按人判配额时的并发锚点（版本号 CAS）
    user_sandbox      这个人名下的一个沙箱：模板 code、镜像、状态（creating / ready / retiring），
                      和基座的 sandbox_instance 共用 sandbox_uuid
    sandbox_binding   沙箱在服务的业务：(biz_type, biz_id) 唯一，一个业务至多绑一个沙箱

★ 沙箱和业务分开记：以后共享（一个沙箱服务同一个人的多个业务）就是一个沙箱多条绑定，表结构不变。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0037_sandbox_provisioning"
down_revision: str | Sequence[str] | None = "0036_thread_credential_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_account",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_uuid", UUID_COL, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_account"),
        sa.UniqueConstraint("user_uuid", name="uk_sandbox_account_user"),
    )
    op.create_table(
        "user_sandbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sandbox_uuid", UUID_COL, nullable=False),
        sa.Column("user_uuid", UUID_COL, nullable=False),
        sa.Column("template_code", sa.String(63), nullable=False),
        sa.Column("image", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="creating"),
        sa.Column("ready_at", TS_COL, nullable=True),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_user_sandbox"),
        sa.UniqueConstraint("sandbox_uuid", name="uk_user_sandbox_uuid"),
    )
    op.create_index("ix_user_sandbox_user", "user_sandbox", ["user_uuid"])
    op.create_table(
        "sandbox_binding",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("binding_uuid", UUID_COL, nullable=False),
        sa.Column("sandbox_uuid", UUID_COL, nullable=False),
        sa.Column("biz_type", sa.String(16), nullable=False),
        sa.Column("biz_id", UUID_COL, nullable=False),
        sa.Column("bound_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_binding"),
        sa.UniqueConstraint("binding_uuid", name="uk_sandbox_binding_uuid"),
        sa.UniqueConstraint("biz_type", "biz_id", name="uk_sandbox_binding_biz"),
    )
    op.create_index("ix_sandbox_binding_sandbox", "sandbox_binding", ["sandbox_uuid"])


def downgrade() -> None:
    op.drop_table("sandbox_binding")
    op.drop_table("user_sandbox")
    op.drop_table("sandbox_account")
