"""应用编码：内网调用开放接口按请求头 X-Caller-App 认应用（内网身份传递 §3、§11）

Revision ID: u0005_app_code
Revises: u0004_tenant
Create Date: 2026-10-19

存量按名称回填：Atlas → atlas，AI TeamFlow → teamflow。其它应用留空（只能用 App Key / Secret），
需要内网调用时由管理员在用户中心填写。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0005_app_code"
down_revision: str | Sequence[str] | None = "u0004_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("uc_app", sa.Column("code", sa.String(32), nullable=True))
    op.create_unique_constraint("uq_uc_app_code", "uc_app", ["code"])
    op.execute("UPDATE uc_app SET code = 'atlas' WHERE name = 'Atlas'")
    op.execute("UPDATE uc_app SET code = 'teamflow' WHERE name = 'AI TeamFlow'")


def downgrade() -> None:
    op.drop_constraint("uq_uc_app_code", "uc_app", type_="unique")
    op.drop_column("uc_app", "code")
