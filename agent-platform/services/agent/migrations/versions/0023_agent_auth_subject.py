"""Agent 的权限类型与数字员工（doc/digital-employee-design.html §5、§7）

Revision ID: 0023_agent_auth_subject
Revises: 0022_approval_modes
Create Date: 2026-10-23

agent.auth_subject          权限类型：user = 以人为权限（X-Auth-Uid = 使用者），
                            agent = 以 Agent 为权限（X-Auth-Uid = 数字员工，只看数字员工的授权）
agent.digital_employee_uid  用户中心里这个 Agent 的数字员工（两种类型都有；回填前为空）
run.auth_subject            这一轮按哪种类型取的 auth_uid（审计与复现；run.auth_uid 迁移 0020 已有）

★ 存量 Agent 一律 user：与 P1 已实现的行为（X-Auth-Uid = uid）完全相同，上线当天没有任何
  Agent 的能力变化。数字员工由 `python -m atlas_agent backfill-agent-identity` 补建。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_agent_auth_subject"
down_revision: str | Sequence[str] | None = "0022_approval_modes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent", sa.Column("auth_subject", sa.String(8), nullable=False, server_default="user")
    )
    # op.f：名字已是最终形态，不再套命名约定（否则成了 ck_agent_ck_agent_…）
    op.create_check_constraint(
        op.f("ck_agent_auth_subject_enum"), "agent", "auth_subject IN ('user','agent')"
    )
    op.add_column("agent", sa.Column("digital_employee_uid", sa.Uuid(), nullable=True))
    op.add_column("run", sa.Column("auth_subject", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "auth_subject")
    op.drop_column("agent", "digital_employee_uid")
    op.drop_constraint(op.f("ck_agent_auth_subject_enum"), "agent", type_="check")
    op.drop_column("agent", "auth_subject")
