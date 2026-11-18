"""应用入口地址：「应用切换」「我的应用」点开去这里（用户中心改版）

Revision ID: u0007_app_home_url
Revises: u0006_digital_employee
Create Date: 2026-11-18

存量按应用编码回填同源入口（网关路径）：atlas → /atlas，teamflow → /teamflow。
其它应用留空（不提供入口），由管理员在「应用 › 接入配置」里填写。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0007_app_home_url"
down_revision: str | Sequence[str] | None = "u0006_digital_employee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uc_app", sa.Column("home_url", sa.String(500), nullable=True))
    op.execute("UPDATE uc_app SET home_url = '/atlas' WHERE code = 'atlas'")
    op.execute("UPDATE uc_app SET home_url = '/teamflow' WHERE code = 'teamflow'")


def downgrade() -> None:
    op.drop_column("uc_app", "home_url")
