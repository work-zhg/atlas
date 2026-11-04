"""thread.git_profile → credential_profiles：会话选的出站凭据档案，通用化成一个列表（改造计划 B4）

Revision ID: 0036_thread_credential_profiles
Revises: 0035_sandbox_base
Create Date: 2026-11-04

c4-model/agent/conversation/code-conversation-thread.html：git_profile 改成凭据档案列表。档案通用化为
注入方式（basic、bearer、api_key）+ 主机（c4-model/agent/code-credentials.html），git over HTTPS 只是
basic 的一种用法。建会话时选定、之后不改，子会话继承。

存量的 git_profile 原样搬成一个元素的列表；降级时取第一个。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL, text_default

revision: str = "0036_thread_credential_profiles"
down_revision: str | Sequence[str] | None = "0035_sandbox_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

thread = sa.table(
    "thread",
    sa.column("id", sa.BigInteger),
    sa.column("git_profile", sa.String(64)),
    sa.column("credential_profiles", JSON_COL),
)


def upgrade() -> None:
    op.add_column("thread", sa.Column("credential_profiles", JSON_COL, nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.select(thread.c.id, thread.c.git_profile)).all()
    for row_id, profile in rows:
        bind.execute(
            sa.update(thread)
            .where(thread.c.id == row_id)
            .values(credential_profiles=[profile] if profile else [])
        )
    op.alter_column(
        "thread",
        "credential_profiles",
        existing_type=JSON_COL,
        nullable=False,
        server_default=text_default("[]"),
    )
    op.drop_column("thread", "git_profile")


def downgrade() -> None:
    op.add_column("thread", sa.Column("git_profile", sa.String(64), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.select(thread.c.id, thread.c.credential_profiles)).all()
    for row_id, profiles in rows:
        if isinstance(profiles, str):  # MySQL 的 JSON 可能按字符串读回
            profiles = json.loads(profiles)
        if profiles:
            bind.execute(
                sa.update(thread).where(thread.c.id == row_id).values(git_profile=profiles[0])
            )
    op.drop_column("thread", "credential_profiles")
