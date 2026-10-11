"""downstream_admission：准入改为由下游节点发起

- 节点状态增加 exited（准出通过，待下游准入）；
- 准入评审挂在下游节点上，审核的是上游各节点当前锁定的版本：artifact_uuid 可空；
- 进行中的旧语义准入（节点自己的准入评审）：取消该轮，节点改为 exited，
  由下游节点重新发起准入（监督器启动时推进进行中的流程）。

Revision ID: t0004
Revises: t0003
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t0004"
down_revision: str | Sequence[str] | None = "t0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('pending','working','exit_review','admit_review','passed','returned')"
_NEW = "status IN ('pending','working','exit_review','exited','admit_review','passed','returned')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tf_process_node_status_enum"), "tf_process_node", type_="check")
    op.create_check_constraint(op.f("ck_tf_process_node_status_enum"), "tf_process_node", _NEW)
    op.alter_column("tf_review_round", "artifact_uuid", existing_type=sa.Uuid(), nullable=True)
    # 旧语义的进行中准入：节点自己在准入评审中 → 取消该轮，改为待下游准入
    op.execute(
        "UPDATE tf_review_round r SET status = 'cancelled', closed_at = now() "
        "FROM tf_process_node n WHERE r.node_uuid = n.uuid AND n.status = 'admit_review' "
        "AND r.stage = 'admit' AND r.status = 'open'"
    )
    op.execute("UPDATE tf_process_node SET status = 'exited' WHERE status = 'admit_review'")


def downgrade() -> None:
    op.execute("UPDATE tf_process_node SET status = 'admit_review' WHERE status = 'exited'")
    op.alter_column("tf_review_round", "artifact_uuid", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint(op.f("ck_tf_process_node_status_enum"), "tf_process_node", type_="check")
    op.create_check_constraint(op.f("ck_tf_process_node_status_enum"), "tf_process_node", _OLD)
