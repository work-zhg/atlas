"""多租户：28 张业务表加 tenant_uuid；名字类的唯一约束改成租户内唯一

Revision ID: 0047_tenant
Revises: 0046_browser
Create Date: 2026-11-19

c4-model/opscenter/design-tenant-isolation.html。业务表都继承 TenantOwned（atlas_tenancy），
查询按当前租户自动过滤。不是租户表的只有 model_catalog（产品自带）和 sandbox_template
（运营中心管）。

已有数据全部归原来的平台租户：它的 uuid 由环境变量 TENANT_BACKFILL_UUID 给出（用户中心里
SELECT uuid FROM uc_tenant WHERE code = 'default'）。表里有数据却没给这个变量，迁移直接失败，
不猜。空库（测试库、新部署）不需要。

唯一约束：
    agent          uq_agent_slug (slug)            → uq_agent_tenant_slug (tenant_uuid, slug)
    service_model  uk_service_model_purpose        → (tenant_uuid, purpose)
    instruction    uk_instruction_owner            → (tenant_uuid, scope, owner_id)
    app_user       ux_app_user_external_id（部分） → (tenant_uuid, external_id)
其余唯一约束都含全局唯一的 uuid（用户、会话、run），不用改。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from atlas_tenancy.migration import add_tenant, drop_tenant

revision: str = "0047_tenant"
down_revision: str | Sequence[str] | None = "0046_browser"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "app_user",
    "agent",
    "agent_version",
    "thread",
    "device",
    "message",
    "message_feedback",
    "run",
    "run_event",
    "run_state",
    "run_file",
    "approval",
    "thread_context",
    "cli_session",
    "skill_usage_daily",
    "thread_memory",
    "memory_user_setting",
    "memory_backfill",
    "service_model",
    "instruction",
    "sandbox_instance",
    "sandbox_event",
    "sandbox_account",
    "user_sandbox",
    "sandbox_binding",
    "sandbox_execution",
    "browser_login",
    "browser_host",
)


def _backfill() -> UUID:
    raw = os.environ.get("TENANT_BACKFILL_UUID", "").strip()
    if raw:
        return UUID(raw)
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(
                f"表 {table} 里已有数据：设置 TENANT_BACKFILL_UUID 为原平台租户的 uuid 再迁移"
                "（用户中心里 SELECT uuid FROM uc_tenant WHERE code = 'default'）"
            )
    return UUID(int=0)  # 空库：没有行要回填


def upgrade() -> None:
    tenant = _backfill()
    for table in TABLES:
        add_tenant(op, table, backfill=tenant)

    op.drop_constraint("uq_agent_slug", "agent", type_="unique")
    op.create_unique_constraint("uq_agent_tenant_slug", "agent", ["tenant_uuid", "slug"])
    op.drop_constraint("uk_service_model_purpose", "service_model", type_="unique")
    op.create_unique_constraint(
        "uk_service_model_purpose", "service_model", ["tenant_uuid", "purpose"]
    )
    op.drop_constraint("uk_instruction_owner", "instruction", type_="unique")
    op.create_unique_constraint(
        "uk_instruction_owner", "instruction", ["tenant_uuid", "scope", "owner_id"]
    )
    op.drop_index("ux_app_user_external_id", table_name="app_user")
    op.create_index(
        "ux_app_user_external_id",
        "app_user",
        ["tenant_uuid", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_app_user_external_id", table_name="app_user")
    op.create_index(
        "ux_app_user_external_id",
        "app_user",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.drop_constraint("uk_instruction_owner", "instruction", type_="unique")
    op.create_unique_constraint("uk_instruction_owner", "instruction", ["scope", "owner_id"])
    op.drop_constraint("uk_service_model_purpose", "service_model", type_="unique")
    op.create_unique_constraint("uk_service_model_purpose", "service_model", ["purpose"])
    op.drop_constraint("uq_agent_tenant_slug", "agent", type_="unique")
    op.create_unique_constraint("uq_agent_slug", "agent", ["slug"])
    for table in reversed(TABLES):
        drop_tenant(op, table)
