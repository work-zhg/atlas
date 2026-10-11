"""会话的 git 凭据档案

Revision ID: 0015_thread_git_profile
Revises: 0014_skill_usage_daily
Create Date: 2026-10-10

acp 会话的 Pod 按档案名挂载 git 凭据（cluster 的 git_profiles；档案本身与谁能用由 cluster 决定）。
这里只存名字，不存任何凭据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_thread_git_profile"
down_revision: str | Sequence[str] | None = "0014_skill_usage_daily"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("git_profile", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("thread", "git_profile")
