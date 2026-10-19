"""数据编码级授权：取代「数据管理员操作码」

Revision ID: u0003_data_type_acl
Revises: u0002_open_api_ext
Create Date: 2026-10-16

角色归角色领域、数据权限归数据权限领域：「管理某编码下全部数据」不再经角色上的操作码
（uc_data_type.admin_operation_code）表达，改为数据编码级授权 uc_data_type_acl。

存量换算：原来经角色 / 岗位持有某编码「数据管理员操作码」的授权对象（用户或部门，含下级
的照旧含下级），换成该编码的 Owner 级数据编码授权 —— 迁移前后能管的数据完全一样。
随后删除这些操作码（角色上的引用随外键级联清掉）与该列。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0003_data_type_acl"
down_revision: str | Sequence[str] | None = "u0002_open_api_ext"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uc_data_type_acl",
        sa.Column("data_type_uuid", sa.Uuid(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("subject_type", sa.String(length=8), nullable=False),
        sa.Column("user_uuid", sa.Uuid(), nullable=True),
        sa.Column("dept_uuid", sa.Uuid(), nullable=True),
        sa.Column("include_sub", sa.Boolean(), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("uuid", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "((subject_type = 'user') = (user_uuid IS NOT NULL))"
            " AND ((subject_type = 'dept') = (dept_uuid IS NOT NULL))",
            name=op.f("ck_uc_data_type_acl_subject"),
        ),
        sa.CheckConstraint(
            "subject_type = 'dept' OR NOT include_sub",
            name=op.f("ck_uc_data_type_acl_sub_only_dept"),
        ),
        sa.CheckConstraint("level IN (1, 2, 3)", name=op.f("ck_uc_data_type_acl_level_enum")),
        sa.ForeignKeyConstraint(
            ["data_type_uuid"],
            ["uc_data_type.uuid"],
            name=op.f("fk_uc_data_type_acl_data_type_uuid"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dept_uuid"],
            ["uc_dept.uuid"],
            name=op.f("fk_uc_data_type_acl_dept_uuid"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_uuid"], ["uc_user.uuid"], name=op.f("fk_uc_data_type_acl_user_uuid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uc_data_type_acl")),
        sa.UniqueConstraint("uuid", name=op.f("uq_uc_data_type_acl_uuid")),
    )
    op.create_index(
        "ix_uc_data_type_acl_dept",
        "uc_data_type_acl",
        ["dept_uuid"],
        unique=False,
        postgresql_where=sa.text("dept_uuid IS NOT NULL"),
    )
    op.create_index(
        "ix_uc_data_type_acl_user",
        "uc_data_type_acl",
        ["user_uuid"],
        unique=False,
        postgresql_where=sa.text("user_uuid IS NOT NULL"),
    )
    op.create_index(
        "uq_uc_data_type_acl",
        "uc_data_type_acl",
        ["data_type_uuid", sa.literal_column("coalesce(user_uuid, dept_uuid)")],
        unique=True,
    )

    # 存量换算：经角色（直接授）或岗位（岗位含该角色）持有数据管理员操作码的授权对象
    # → 该编码的 Owner 级数据编码授权。同一对象多条来源时，含下级优先（范围取大）。
    op.execute(
        """
        INSERT INTO uc_data_type_acl
            (uuid, data_type_uuid, level, subject_type, user_uuid, dept_uuid, include_sub,
             granted_by, granted_at)
        SELECT DISTINCT ON (dt.uuid, coalesce(g.user_uuid, g.dept_uuid))
               gen_random_uuid(), dt.uuid, 3, g.subject_type, g.user_uuid, g.dept_uuid,
               g.include_sub, g.granted_by, now()
          FROM uc_data_type dt
          JOIN uc_operation o
            ON o.app_uuid = dt.app_uuid AND o.code = dt.admin_operation_code
          JOIN uc_role_operation ro ON ro.operation_uuid = o.uuid
          JOIN uc_grant g
            ON (g.kind = 'role' AND g.role_uuid = ro.role_uuid)
            OR (g.kind = 'position' AND g.position_uuid IN (
                    SELECT pr.position_uuid FROM uc_position_role pr
                     WHERE pr.role_uuid = ro.role_uuid))
         WHERE dt.admin_operation_code IS NOT NULL
         ORDER BY dt.uuid, coalesce(g.user_uuid, g.dept_uuid), g.include_sub DESC
        """
    )
    # 这些操作码只为「数据管理员」而存在；角色上的引用随 uc_role_operation 的外键级联删除
    op.execute(
        """
        DELETE FROM uc_operation o
         USING uc_data_type dt
         WHERE o.app_uuid = dt.app_uuid AND o.code = dt.admin_operation_code
        """
    )
    op.drop_column("uc_data_type", "admin_operation_code")


def downgrade() -> None:
    op.add_column("uc_data_type", sa.Column("admin_operation_code", sa.String(64), nullable=True))
    op.drop_index("uq_uc_data_type_acl", table_name="uc_data_type_acl")
    op.drop_index("ix_uc_data_type_acl_user", table_name="uc_data_type_acl")
    op.drop_index("ix_uc_data_type_acl_dept", table_name="uc_data_type_acl")
    op.drop_table("uc_data_type_acl")
