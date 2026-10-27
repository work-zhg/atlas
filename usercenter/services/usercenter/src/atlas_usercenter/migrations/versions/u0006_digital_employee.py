"""数字员工：uc_user 增加用户类型（数字员工设计 §3.2）

Revision ID: u0006_digital_employee
Revises: u0005_app_code
Create Date: 2026-10-23

数字员工就是一种用户：授权、数据权限、审计全部复用。差别只在这几列：
  user_type        human | digital（存量全部是 human）
  owner_uuid       负责人（必须是人）
  source_app_uuid  创建它的应用
  external_ref     来源应用里的对象 id（Atlas = agent id），幂等创建的依据
  description
email、dept_uuid 改为可空：数字员工没有邮箱，也不进组织树（约束保证人必须有、数字员工必须没有部门）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0006_digital_employee"
down_revision: str | Sequence[str] | None = "u0005_app_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "uc_user",
        sa.Column("user_type", sa.String(8), nullable=False, server_default="human"),
    )
    op.add_column("uc_user", sa.Column("owner_uuid", sa.Uuid(), nullable=True))
    op.add_column("uc_user", sa.Column("source_app_uuid", sa.Uuid(), nullable=True))
    op.add_column("uc_user", sa.Column("external_ref", sa.String(64), nullable=True))
    op.add_column("uc_user", sa.Column("description", sa.String(200), nullable=True))
    op.alter_column("uc_user", "email", nullable=True)
    op.alter_column("uc_user", "dept_uuid", nullable=True)

    op.create_check_constraint(
        op.f("ck_uc_user_user_type_enum"), "uc_user", "user_type IN ('human','digital')"
    )
    op.create_check_constraint(
        op.f("ck_uc_user_human_email"), "uc_user", "user_type = 'digital' OR email IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("ck_uc_user_digital_no_dept"),
        "uc_user",
        "(user_type = 'digital') = (dept_uuid IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_uc_user_digital_owner_app"),
        "uc_user",
        "(user_type = 'digital') = (owner_uuid IS NOT NULL AND source_app_uuid IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_uc_user_owner",
        "uc_user",
        "uc_user",
        ["tenant_uuid", "owner_uuid"],
        ["tenant_uuid", "uuid"],
    )
    op.create_foreign_key(
        op.f("fk_uc_user_source_app_uuid"), "uc_user", "uc_app", ["source_app_uuid"], ["uuid"]
    )
    op.create_index(
        "uq_uc_user_digital_ref",
        "uc_user",
        ["tenant_uuid", "source_app_uuid", "external_ref"],
        unique=True,
        postgresql_where=sa.text("user_type = 'digital'"),
    )
    op.create_index("ix_uc_user_type_status", "uc_user", ["user_type", "status"])


def downgrade() -> None:
    # 有数字员工时回不去：email / dept_uuid 恢复 NOT NULL 会失败 —— 先删掉它们
    op.execute(
        "DELETE FROM uc_grant WHERE user_uuid IN (SELECT uuid FROM uc_user WHERE user_type = 'digital')"
    )
    op.execute(
        "DELETE FROM uc_data_acl WHERE user_uuid IN"
        " (SELECT uuid FROM uc_user WHERE user_type = 'digital')"
    )
    op.execute("DELETE FROM uc_user WHERE user_type = 'digital'")
    op.drop_index("ix_uc_user_type_status", table_name="uc_user")
    op.drop_index("uq_uc_user_digital_ref", table_name="uc_user")
    op.drop_constraint(op.f("fk_uc_user_source_app_uuid"), "uc_user", type_="foreignkey")
    op.drop_constraint("fk_uc_user_owner", "uc_user", type_="foreignkey")
    for name in ("digital_owner_app", "digital_no_dept", "human_email", "user_type_enum"):
        op.drop_constraint(op.f(f"ck_uc_user_{name}"), "uc_user", type_="check")
    op.alter_column("uc_user", "dept_uuid", nullable=False)
    op.alter_column("uc_user", "email", nullable=False)
    for col in ("description", "external_ref", "source_app_uuid", "owner_uuid", "user_type"):
        op.drop_column("uc_user", col)
