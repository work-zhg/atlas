"""沙箱 · 命令执行：执行账本进库（sandbox_execution）

Revision ID: 0040_sandbox_execution
Revises: 0039_sandbox_sharing
Create Date: 2026-11-10

agent-platform/doc/design/sandbox-defect-fixes.html D3：执行账本（工具调用 → 沙箱里的命令）原来
在 Redis，而且拿到命令 id 之后才写 —— 实例死在「请求已到 execd、账本还没写」之间，接管重放会再
提交一次。
现在提交之前先落一行「提交中」（cmd_id 为空），拿到 id 再补上；放库里，Redis 丢数据也不会重跑。

★ 不迁 Redis 里的旧记录：它们 24 小时过期，部署时正在跑的命令接管后最多重跑一次（与旧版本一样）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0040_sandbox_execution"
down_revision: str | Sequence[str] | None = "0039_sandbox_sharing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_execution",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("execution_uuid", UUID_COL, nullable=False),
        sa.Column("run_uuid", UUID_COL, nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("sandbox_uuid", UUID_COL, nullable=False),
        sa.Column("cmd_id", sa.String(128), nullable=True),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_execution"),
        sa.UniqueConstraint("execution_uuid", name="uk_sandbox_execution_uuid"),
        sa.UniqueConstraint("run_uuid", "tool_call_id", name="uk_sandbox_execution_call"),
    )


def downgrade() -> None:
    op.drop_table("sandbox_execution")
