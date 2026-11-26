"""会话改成刷新令牌（c4-model/apigateway/code-auth.html）：登录发访问 JWT + 刷新令牌

Revision ID: u0009_refresh_token
Revises: u0008_no_platform_tenant
Create Date: 2026-11-26

· uc_session 一行 = 一个刷新令牌：加 prev_token_hash、rotated_at（轮换后的宽限期、发现被盗用）。
· 存量会话照样能用：旧网关的 verify 还认 uc_session Cookie；它们的 token_hash 不变，到期自然失效。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0009_refresh_token"
down_revision: str | Sequence[str] | None = "u0008_no_platform_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uc_session", sa.Column("prev_token_hash", sa.String(64), nullable=True))
    op.add_column("uc_session", sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_uc_session_prev_token_hash", "uc_session", ["prev_token_hash"])


def downgrade() -> None:
    op.drop_index("ix_uc_session_prev_token_hash", table_name="uc_session")
    op.drop_column("uc_session", "rotated_at")
    op.drop_column("uc_session", "prev_token_hash")
