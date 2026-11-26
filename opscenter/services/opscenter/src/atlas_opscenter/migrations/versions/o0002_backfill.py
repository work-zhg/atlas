"""租户操作加一种：BACKFILL（给新登记的系统补建）

Revision ID: o0002_backfill
Revises: o0001_initial
Create Date: 2026-11-25

★ o0001 建的约束名被命名约定套了两层（ck_ops_tenant_operation_ck_ops_tenant_operation_kind_enum）：
  按实际的名字删（两种写法都试，没有就跳过），新的用模型里的名字，由命名约定生成
  ck_ops_tenant_operation_kind_enum。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "o0002_backfill"
down_revision: str | Sequence[str] | None = "o0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ops_tenant_operation"
_DOUBLED = "ck_ops_tenant_operation_ck_ops_tenant_operation_kind_enum"
_SINGLE = "ck_ops_tenant_operation_kind_enum"


def _drop_kind_check() -> None:
    for name in (_DOUBLED, _SINGLE):
        op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')


def upgrade() -> None:
    _drop_kind_check()
    op.create_check_constraint(
        "kind_enum", _TABLE, "kind IN ('OPEN','SUSPEND','RESUME','CLOSE','BACKFILL')"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM ops_operation_step WHERE operation_uuid IN "
        f"(SELECT operation_uuid FROM {_TABLE} WHERE kind = 'BACKFILL')"
    )
    op.execute(f"DELETE FROM {_TABLE} WHERE kind = 'BACKFILL'")
    _drop_kind_check()
    op.create_check_constraint("kind_enum", _TABLE, "kind IN ('OPEN','SUSPEND','RESUME','CLOSE')")
