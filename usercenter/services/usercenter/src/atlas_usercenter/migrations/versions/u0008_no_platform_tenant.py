"""去掉平台租户：所有租户一律平等（租户隔离设计 §0、§11）

Revision ID: u0008_no_platform_tenant
Revises: u0007_app_home_url
Create Date: 2026-11-20

· uc_tenant.kind 删除：原来的平台租户变成普通租户，企业代码、名字、数据都不变。
· 新增 uc_tenant.admin_uuid：运营中心建租户时的首个管理员；他登录之前可以补发临时密码。
  存量租户为空（不能补发：它们的管理员早就登录过了）。
· 平台级操作码 app:manage、tenant:view、tenant:manage 和菜单 tenants 不在这里删：migrate
  之后的 sync_builtin 按代码同步，删掉目录里没有的（角色上的绑定随外键级联删除）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0008_no_platform_tenant"
down_revision: str | Sequence[str] | None = "u0007_app_home_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_uc_tenant_platform", table_name="uc_tenant")
    op.drop_constraint("ck_uc_tenant_kind_enum", "uc_tenant", type_="check")
    op.drop_column("uc_tenant", "kind")
    op.add_column("uc_tenant", sa.Column("admin_uuid", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("uc_tenant", "admin_uuid")
    op.add_column(
        "uc_tenant",
        sa.Column("kind", sa.String(16), nullable=False, server_default="customer"),
    )
    # 最早的租户当回平台租户（升级前它就是迁移 u0004 建的 default）
    op.execute("UPDATE uc_tenant SET kind = 'platform' WHERE id = (SELECT min(id) FROM uc_tenant)")
    op.alter_column("uc_tenant", "kind", server_default=None)
    op.create_check_constraint(
        "ck_uc_tenant_kind_enum", "uc_tenant", "kind IN ('platform','customer')"
    )
    op.create_index(
        "uq_uc_tenant_platform",
        "uc_tenant",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("kind = 'platform'"),
    )
