"""开放接口扩展：登录日志记录应用、数据编码的数据管理员操作码

Revision ID: u0002_open_api_ext
Revises: u0001_initial
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0002_open_api_ext"
down_revision: str | Sequence[str] | None = "u0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uc_login_log", sa.Column("app_name", sa.String(50), nullable=True))
    op.add_column("uc_data_type", sa.Column("admin_operation_code", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("uc_data_type", "admin_operation_code")
    op.drop_column("uc_login_log", "app_name")
