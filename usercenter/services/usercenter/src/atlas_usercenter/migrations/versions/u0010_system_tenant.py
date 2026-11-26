"""系统租户与「仅系统租户」的应用（c4-model/opscenter/design-opscenter.html）

Revision ID: u0010_system_tenant
Revises: u0009_refresh_token
Create Date: 2026-11-26

· uc_tenant.is_system：运营团队所在的租户。只能用部署命令建（python -m atlas_usercenter
  bootstrap-system）；不能停用、删除、设到期。业务系统不知道这个标记。
· uc_app.system_only：只能开给系统租户（运营中心）；别的租户的人取授权快照一律不可访问。
· 存量全为 false：现有租户（包括原来的平台租户）都是普通租户。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0010_system_tenant"
down_revision: str | Sequence[str] | None = "u0009_refresh_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "uc_tenant",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "uc_app",
        sa.Column("system_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("uc_app", "system_only")
    op.drop_column("uc_tenant", "is_system")
