"""租户设置：平台指令改成租户指令（scope platform → tenant）

Revision ID: 0049_tenant_instruction
Revises: 0048_capacity
Create Date: 2026-11-19

c4-model/agent/code-tenant-settings.html。没有平台租户以后，「对所有人生效」的指令只能对一个
租户的所有人生效：原来唯一的那份平台指令在 0047 里已经归到原平台租户（TENANT_BACKFILL_UUID），
这里只改 scope 的取值。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_tenant_instruction"
down_revision: str | Sequence[str] | None = "0048_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 0027 建的约束名被命名约定又加了一次前缀
_OLD_CK = "ck_instruction_ck_instruction_scope_enum"
_NEW_CK = "ck_instruction_scope_enum"


def upgrade() -> None:
    op.drop_constraint(op.f(_OLD_CK), "instruction", type_="check")
    op.execute(sa.text("UPDATE instruction SET scope = 'tenant' WHERE scope = 'platform'"))
    op.create_check_constraint(op.f(_NEW_CK), "instruction", "scope IN ('tenant','user')")


def downgrade() -> None:
    op.drop_constraint(op.f(_NEW_CK), "instruction", type_="check")
    op.execute(sa.text("UPDATE instruction SET scope = 'platform' WHERE scope = 'tenant'"))
    op.create_check_constraint(op.f(_OLD_CK), "instruction", "scope IN ('platform','user')")
