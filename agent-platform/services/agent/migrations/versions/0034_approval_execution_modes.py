"""approval.execution_modes：计划审批登记时允许的执行模式（改造计划 A3）

Revision ID: 0034_approval_execution_modes
Revises: 0033_thread_context
Create Date: 2026-11-03

批准计划要选执行模式，而且必须在允许的范围里（模式是权限）。原先做决定时回头查这个 run 用的
智能体版本；现在登记时记下，校验不依赖别的组件，作者之后收窄了配置，这次计划也按提交那一刻的
范围（c4-model/agent/code-approval.html）。

★ 还挂着的计划审批按它所在 run 的智能体版本回填 —— 否则迁移之后它们一个模式都选不了、永远
  批不下来。已经有结果的不回填（不会再做决定）。规则与 ApprovalSpec.execution_modes 一致：
  允许的模式里除了规划之外的；快照里没有审批配置就是默认的全部三种。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL_NULLABLE

revision: str = "0034_approval_execution_modes"
down_revision: str | Sequence[str] | None = "0033_thread_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_MODES = ("plan", "manual", "auto")


def _execution_modes(spec: object) -> list[str]:
    if isinstance(spec, str):
        spec = json.loads(spec)
    approval = (spec or {}).get("approval") if isinstance(spec, dict) else None
    allowed = (approval or {}).get("allowed_modes") or _ALL_MODES
    return [m for m in _ALL_MODES if m in allowed and m != "plan"]


def upgrade() -> None:
    op.add_column("approval", sa.Column("execution_modes", JSON_COL_NULLABLE, nullable=True))
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT a.id, v.spec FROM approval a "
            "JOIN run r ON r.run_uuid = a.run_uuid "
            "JOIN agent_version v ON v.agent_version_uuid = r.agent_version_uuid "
            "WHERE a.kind = 'plan' AND a.status = 'pending'"
        )
    ).all()
    update = sa.text("UPDATE approval SET execution_modes = :modes WHERE id = :id").bindparams(
        sa.bindparam("modes", type_=JSON_COL_NULLABLE)
    )
    for row_id, spec in rows:
        bind.execute(update, {"id": row_id, "modes": _execution_modes(spec)})


def downgrade() -> None:
    op.drop_column("approval", "execution_modes")
