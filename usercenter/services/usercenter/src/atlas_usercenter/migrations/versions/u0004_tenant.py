"""租户：用户、组织属于某个租户（租户设计 §05 · §13）

Revision ID: u0004_tenant
Revises: u0003_data_type_acl
Create Date: 2026-10-16

存量 = 一个平台租户（code = default，kind = platform）：现有超级管理员同时是平台管理员，
单租户部署的使用方式不变。

  · uc_tenant、uc_tenant_app（租户开通应用；可访问范围从应用移到这里）
  · 租户表加 tenant_uuid，回填平台租户
  · 全局唯一改为租户内唯一（账号、邮箱、岗位、数据对象、根部门、角色编码）
  · 租户表之间的外键改为复合外键 (tenant_uuid, xxx_uuid)：跨租户引用写不进去
  · 接口令牌改为绑定 (应用, 租户)：存量令牌作废，应用下次调用自动重新换取
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u0004_tenant"
down_revision: str | Sequence[str] | None = "u0003_data_type_acl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"

#: 必须有租户的表（回填后 NOT NULL）
_OWNED = (
    "uc_dept",
    "uc_user",
    "uc_app_scope_dept",
    "uc_position",
    "uc_position_role",
    "uc_grant",
    "uc_data_object",
    "uc_data_acl",
    "uc_data_type_acl",
)


def _audited() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
    ]


def upgrade() -> None:
    # ── 租户
    op.create_table(
        "uc_tenant",
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("max_users", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_audited(),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("uuid", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('platform','customer')", name="ck_uc_tenant_kind_enum"),
        sa.CheckConstraint(
            "status IN ('active','suspended','closed')", name="ck_uc_tenant_status_enum"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_uc_tenant"),
        sa.UniqueConstraint("uuid", name="uq_uc_tenant_uuid"),
        sa.UniqueConstraint("code", name="uq_uc_tenant_code"),
    )
    op.create_index(
        "uq_uc_tenant_platform",
        "uc_tenant",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("kind = 'platform'"),
    )
    op.execute(
        """
        INSERT INTO uc_tenant (uuid, code, name, kind, status, created_at, updated_at, version)
        SELECT gen_random_uuid(), 'default',
               coalesce((SELECT name FROM uc_dept WHERE parent_uuid IS NULL LIMIT 1), '默认租户'),
               'platform', 'active', now(), now(), 1
        """
    )
    platform = "(SELECT uuid FROM uc_tenant WHERE kind = 'platform')"

    # ── 租户开通应用：可访问范围从应用移到「租户 × 应用」
    op.create_table(
        "uc_tenant_app",
        sa.Column("tenant_uuid", sa.Uuid(), nullable=False),
        sa.Column("app_uuid", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("scope_all", sa.Boolean(), nullable=False),
        sa.Column("enabled_by", sa.Uuid(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("uuid", sa.Uuid(), nullable=False),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_uc_tenant_app_status_enum"),
        sa.ForeignKeyConstraint(
            ["tenant_uuid"], ["uc_tenant.uuid"], name="fk_uc_tenant_app_tenant_uuid"
        ),
        sa.ForeignKeyConstraint(
            ["app_uuid"], ["uc_app.uuid"], name="fk_uc_tenant_app_app_uuid", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_uc_tenant_app"),
        sa.UniqueConstraint("uuid", name="uq_uc_tenant_app_uuid"),
        sa.UniqueConstraint("tenant_uuid", "app_uuid", name="uq_uc_tenant_app"),
    )
    op.execute(
        f"""
        INSERT INTO uc_tenant_app (uuid, tenant_uuid, app_uuid, status, scope_all, enabled_at)
        SELECT gen_random_uuid(), {platform}, uuid, 'active', scope_all, now()
          FROM uc_app WHERE NOT is_builtin
        """
    )
    op.drop_column("uc_app", "scope_all")

    # ── 租户列：先加可空 → 回填平台租户 → NOT NULL
    for table in _OWNED:
        op.add_column(table, sa.Column("tenant_uuid", sa.Uuid(), nullable=True))
        op.execute(f"UPDATE {table} SET tenant_uuid = {platform}")
        op.alter_column(table, "tenant_uuid", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_tenant_uuid", table, "uc_tenant", ["tenant_uuid"], ["uuid"]
        )
    # 角色：存量全部是应用内置（共用），租户列保持为空
    op.add_column("uc_role", sa.Column("tenant_uuid", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_uc_role_tenant_uuid", "uc_role", "uc_tenant", ["tenant_uuid"], ["uuid"]
    )
    # 日志与审计：显式记录，可空
    for table in ("uc_login_log", "uc_audit_event"):
        op.add_column(table, sa.Column("tenant_uuid", sa.Uuid(), nullable=True))
        op.execute(f"UPDATE {table} SET tenant_uuid = {platform}")
    op.create_index("ix_uc_login_log_tenant_uuid", "uc_login_log", ["tenant_uuid"])
    op.create_index(
        "ix_uc_audit_event_tenant", "uc_audit_event", ["tenant_uuid", sa.text("occurred_at DESC")]
    )
    # 接口令牌绑定租户：存量作废
    op.execute("DELETE FROM uc_app_token")
    op.add_column("uc_app_token", sa.Column("tenant_uuid", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "fk_uc_app_token_tenant_uuid",
        "uc_app_token",
        "uc_tenant",
        ["tenant_uuid"],
        ["uuid"],
        ondelete="CASCADE",
    )

    # ── 唯一性：全局 → 租户内
    op.drop_constraint("uq_uc_user_account", "uc_user", type_="unique")
    op.drop_constraint("uq_uc_user_email", "uc_user", type_="unique")
    op.create_unique_constraint("uq_uc_user_account", "uc_user", ["tenant_uuid", "account"])
    op.create_unique_constraint("uq_uc_user_email", "uc_user", ["tenant_uuid", "email"])
    op.drop_index("uq_uc_dept_single_root", table_name="uc_dept")
    op.create_index(
        "uq_uc_dept_single_root",
        "uc_dept",
        ["tenant_uuid"],
        unique=True,
        postgresql_where=sa.text("parent_uuid IS NULL"),
    )
    op.drop_constraint("uq_uc_position_code", "uc_position", type_="unique")
    op.drop_constraint("uq_uc_position_name", "uc_position", type_="unique")
    op.create_unique_constraint("uq_uc_position_code", "uc_position", ["tenant_uuid", "code"])
    op.create_unique_constraint("uq_uc_position_name", "uc_position", ["tenant_uuid", "name"])
    op.drop_constraint("uq_uc_data_object", "uc_data_object", type_="unique")
    op.create_unique_constraint(
        "uq_uc_data_object", "uc_data_object", ["tenant_uuid", "data_type_uuid", "data_id"]
    )
    op.drop_index("uq_uc_data_type_acl", table_name="uc_data_type_acl")
    op.create_index(
        "uq_uc_data_type_acl",
        "uc_data_type_acl",
        ["tenant_uuid", "data_type_uuid", sa.literal_column("coalesce(user_uuid, dept_uuid)")],
        unique=True,
    )
    op.drop_constraint("uq_uc_role_code", "uc_role", type_="unique")
    op.drop_constraint("uq_uc_role_name", "uc_role", type_="unique")
    op.create_index(
        "uq_uc_role_code",
        "uc_role",
        [sa.literal_column(f"coalesce(tenant_uuid, {_ZERO})"), "code"],
        unique=True,
    )
    op.create_index(
        "uq_uc_role_name",
        "uc_role",
        [sa.literal_column(f"coalesce(tenant_uuid, {_ZERO})"), "app_uuid", "name"],
        unique=True,
    )

    # ── 复合外键：被引用方先有 (tenant_uuid, uuid) 唯一
    for table in ("uc_dept", "uc_user", "uc_position", "uc_data_object"):
        op.create_unique_constraint(f"{table}_tenant_uuid_key", table, ["tenant_uuid", "uuid"])
    # 约束名与模型一致
    op.execute(
        "ALTER TABLE uc_dept RENAME CONSTRAINT uc_dept_tenant_uuid_key TO uq_uc_dept_tenant_uuid"
    )
    op.execute(
        "ALTER TABLE uc_user RENAME CONSTRAINT uc_user_tenant_uuid_key TO uq_uc_user_tenant_uuid"
    )
    op.execute(
        "ALTER TABLE uc_position RENAME CONSTRAINT uc_position_tenant_uuid_key"
        " TO uq_uc_position_tenant_uuid"
    )
    op.execute(
        "ALTER TABLE uc_data_object RENAME CONSTRAINT uc_data_object_tenant_uuid_key"
        " TO uq_uc_data_object_tenant_uuid"
    )

    # (表, 旧外键, 新外键, 列, 被引用表, ondelete)
    composite = [
        ("uc_dept", "fk_uc_dept_parent_uuid", "fk_uc_dept_parent", "parent_uuid", "uc_dept", None),
        (
            "uc_dept",
            "fk_uc_dept_leader_uuid",
            "fk_uc_dept_leader_uuid",
            "leader_uuid",
            "uc_user",
            None,
        ),
        ("uc_user", "fk_uc_user_dept_uuid", "fk_uc_user_dept", "dept_uuid", "uc_dept", None),
        (
            "uc_app_scope_dept",
            "fk_uc_app_scope_dept_dept_uuid",
            "fk_uc_app_scope_dept_dept",
            "dept_uuid",
            "uc_dept",
            "CASCADE",
        ),
        (
            "uc_position_role",
            "fk_uc_position_role_position_uuid",
            "fk_uc_position_role_position",
            "position_uuid",
            "uc_position",
            "CASCADE",
        ),
        (
            "uc_grant",
            "fk_uc_grant_position_uuid",
            "fk_uc_grant_position",
            "position_uuid",
            "uc_position",
            None,
        ),
        ("uc_grant", "fk_uc_grant_user_uuid", "fk_uc_grant_user", "user_uuid", "uc_user", None),
        (
            "uc_grant",
            "fk_uc_grant_dept_uuid",
            "fk_uc_grant_dept",
            "dept_uuid",
            "uc_dept",
            "CASCADE",
        ),
        (
            "uc_data_acl",
            "fk_uc_data_acl_data_object_uuid",
            "fk_uc_data_acl_object",
            "data_object_uuid",
            "uc_data_object",
            "CASCADE",
        ),
        (
            "uc_data_acl",
            "fk_uc_data_acl_user_uuid",
            "fk_uc_data_acl_user",
            "user_uuid",
            "uc_user",
            None,
        ),
        (
            "uc_data_acl",
            "fk_uc_data_acl_dept_uuid",
            "fk_uc_data_acl_dept",
            "dept_uuid",
            "uc_dept",
            "CASCADE",
        ),
        (
            "uc_data_type_acl",
            "fk_uc_data_type_acl_user_uuid",
            "fk_uc_data_type_acl_user",
            "user_uuid",
            "uc_user",
            None,
        ),
        (
            "uc_data_type_acl",
            "fk_uc_data_type_acl_dept_uuid",
            "fk_uc_data_type_acl_dept",
            "dept_uuid",
            "uc_dept",
            "CASCADE",
        ),
    ]
    for table, old, new, column, target, ondelete in composite:
        op.drop_constraint(old, table, type_="foreignkey")
        op.create_foreign_key(
            new,
            table,
            target,
            ["tenant_uuid", column],
            ["tenant_uuid", "uuid"],
            ondelete=ondelete,
        )


def downgrade() -> None:
    raise NotImplementedError("租户迁移不提供降级：请从备份恢复")
