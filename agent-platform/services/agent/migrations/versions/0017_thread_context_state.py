"""上下文管理的会话级状态（doc/context-management-design.html §17）

Revision ID: 0017_thread_context_state
Revises: 0016_auth_identity
Create Date: 2026-10-13

thread.context_state：自动压缩连续失败次数（熔断）等会话级状态。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL_NULLABLE

revision: str = "0017_thread_context_state"
down_revision: str | Sequence[str] | None = "0016_auth_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 可空：MySQL 的 JSON 列不支持字面量默认值；NULL 在代码里按空字典处理
    op.add_column("thread", sa.Column("context_state", JSON_COL_NULLABLE, nullable=True))


def downgrade() -> None:
    op.drop_column("thread", "context_state")
