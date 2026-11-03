"""approval：一个调用至多一件待决的事（改造计划 A2）

Revision ID: 0032_approval_call_key
Revises: 0031_cli_session
Create Date: 2026-11-03

uk_approval_call (run_uuid, tool_call_id)：原生和 CLI 的审批 id 都由 (run, 调用 id) 派生，重放、
续跑、接管、bridge 补发都落到同一行，不会弹出两个窗口
（c4-model/agent/code-cli-runtime-permission.html）。
此前 CLI 的审批 id 是现生成的，换实例接管后同一个调用会再登记一行；建约束前把这种重复留最新的一行。
tool_call_id 为空的（旧数据）不参与唯一。

ix_approval_run_status (run_uuid, status)：续跑前查待决事项。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from atlas_agent.db.migration_types import dialect

revision: str = "0032_approval_call_key"
down_revision: str | Sequence[str] | None = "0031_cli_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if dialect() in ("mysql", "mariadb"):
        op.execute(
            "DELETE a FROM approval a JOIN approval b "
            "ON a.run_uuid = b.run_uuid AND a.tool_call_id = b.tool_call_id AND a.id < b.id"
        )
    else:
        op.execute(
            "DELETE FROM approval a USING approval b "
            "WHERE a.run_uuid = b.run_uuid AND a.tool_call_id = b.tool_call_id AND a.id < b.id"
        )
    op.create_unique_constraint("uk_approval_call", "approval", ["run_uuid", "tool_call_id"])
    op.create_index("ix_approval_run_status", "approval", ["run_uuid", "status"])


def downgrade() -> None:
    op.drop_index("ix_approval_run_status", table_name="approval")
    op.drop_constraint("uk_approval_call", "approval", type_="unique")
