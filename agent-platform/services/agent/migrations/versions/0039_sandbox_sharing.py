"""沙箱 · 按人共享：user_sandbox.share_key、sandbox_binding.slot

Revision ID: 0039_sandbox_sharing
Revises: 0038_run_queued_at
Create Date: 2026-11-09

c4-model/agent/code-workspace.html、dynamic/dynamic-subagent.html「工作区与沙箱原理」：同一个人的会话
共用沙箱。

    user_sandbox.share_key   共享键（业务类型|模板|镜像|业务给的键）：相同的可以共用；NULL = 独占
    sandbox_binding.slot     沙箱内的槽位号：同一个沙箱里各不相同，CLI Agent 按它分 bridge 的端口

★ 已有的沙箱各只有一条绑定（slot 为 0），唯一约束直接成立。
★ 已有的沙箱一律标成 retiring：它们是旧布局（按会话挂 /workspace、/skills，CLI 的 bridge 是
  入口进程），和新布局（整个 {user}/ 挂到 /data、bridge 按会话拉起）不兼容，不能接着用。
  迁移只改库，Pod 由维护者的通用供给对账删掉（重试卡在 RETIRING 的）；下一次确保时绑在上面的
  业务解绑、分配新的。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_sandbox_sharing"
down_revision: str | Sequence[str] | None = "0038_run_queued_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_sandbox", sa.Column("share_key", sa.String(512), nullable=True))
    op.add_column(
        "sandbox_binding",
        sa.Column("slot", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_unique_constraint(
        "uk_sandbox_binding_slot", "sandbox_binding", ["sandbox_uuid", "slot"]
    )
    op.execute(sa.text("UPDATE user_sandbox SET status = 'retiring'"))


def downgrade() -> None:
    op.drop_constraint("uk_sandbox_binding_slot", "sandbox_binding", type_="unique")
    op.drop_column("sandbox_binding", "slot")
    op.drop_column("user_sandbox", "share_key")
