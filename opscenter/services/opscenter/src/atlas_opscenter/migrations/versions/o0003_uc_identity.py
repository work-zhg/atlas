"""运营人员改由用户中心管（c4-model/opscenter/design-opscenter.html）：去掉自己的账号和会话，审计记模块

Revision ID: o0003_uc_identity
Revises: o0002_backfill
Create Date: 2026-11-26

· 删 ops_operator、ops_session：运营人员是用户中心里系统租户的人，统一登录，按模块授权。
  旧审计里的 operator_uuid 指向已删的运营账号；名字有快照，照样看得懂。
· ops_audit 加 module（模块的数据编码）：查审计时能按模块筛。升级前的记录为空。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o0003_uc_identity"
down_revision: str | Sequence[str] | None = "o0002_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("ops_session")
    op.drop_table("ops_operator")
    op.add_column("ops_audit", sa.Column("module", sa.String(32), nullable=True))
    op.execute("CREATE INDEX ix_ops_audit_module ON ops_audit (module, at DESC)")


def downgrade() -> None:
    raise NotImplementedError("运营账号已经改由用户中心管，不能退回去")
