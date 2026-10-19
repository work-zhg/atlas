"""服务账号去掉「管理全部」操作码（数据编码级授权取代，见用户中心 u0003）

Revision ID: 0019_drop_manage_all_ops
Revises: 0018_run_lease
Create Date: 2026-10-16

agent:manage_all、mcp:manage_all 不再是操作码 ——「管理全部智能体 / MCP 服务」属于数据权限，
在用户中心以数据编码级授权表达。服务账号不是用户中心的人员，本来就拿不到数据权限，
存量里带着这两个码的只是无效残留；不清掉的话编辑这些服务账号时会被目录校验拒绝。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_drop_manage_all_ops"
down_revision: str | Sequence[str] | None = "0018_run_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REMOVED = {"agent:manage_all", "mcp:manage_all"}


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, operations FROM service_account")).all()
    for row_id, ops in rows:
        current = ops if isinstance(ops, list) else json.loads(ops or "[]")
        kept = [o for o in current if o not in _REMOVED]
        if kept != current:
            conn.execute(
                sa.text("UPDATE service_account SET operations = :ops WHERE id = :id").bindparams(
                    sa.bindparam("ops", type_=sa.JSON)
                ),
                {"ops": kept, "id": row_id},
            )


def downgrade() -> None:
    """不恢复：被删掉的是无效残留。"""
