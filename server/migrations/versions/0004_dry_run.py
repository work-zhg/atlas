"""dry-run：run 可以不绑定已保存的 agent_version

Revision ID: 0004_dry_run
Revises: 0003_message_clock_timestamp
Create Date: 2026-08-20

试跑（§11 `POST /agents/{id}/dry-run`）用的是编辑器里**尚未保存**的草稿 spec，
所以没有对应的 agent_version 行。

三个可选做法里选了「加快照列」：
  - 为试跑建一条临时 agent_version：会污染用户可见的版本历史（§5.4），
    「保存 = 新版本」这条语义就乱了
  - 完全不建 run 行：GET /runs/{id}/events 与取消都要查 run，会一起失效
  - ★ agent_version_id 可空 + spec_snapshot 存草稿：正式 run 走前者，
    试跑走后者，CHECK 保证恰好有一个

thread_id 早已可空（NULL = dry-run），本次补齐 spec 侧。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import JSON_COL_NULLABLE, UUID_COL

revision: str = "0004_dry_run"
down_revision: str | Sequence[str] | None = "0003_message_clock_timestamp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("run", "agent_version_id", existing_type=UUID_COL, nullable=True)
    # spec_snapshot 与 agent_version.spec 同构（都是 AgentSpecIn 的 JSON 形态）。
    # 输入另立一列而不是塞进 spec_snapshot —— 后者的名字只承诺装 spec。
    op.add_column("run", sa.Column("spec_snapshot", JSON_COL_NULLABLE, nullable=True))
    op.add_column("run", sa.Column("input_snapshot", JSON_COL_NULLABLE, nullable=True))

    # 恰好有一个来源 —— 两个都空会让执行器拿不到 spec，两个都有则语义不明
    op.create_check_constraint(
        "run_spec_source",
        "run",
        "(agent_version_id IS NOT NULL) <> (spec_snapshot IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("run_spec_source", "run", type_="check")
    op.execute("DELETE FROM run WHERE agent_version_id IS NULL")
    op.drop_column("run", "input_snapshot")
    op.drop_column("run", "spec_snapshot")
    op.alter_column("run", "agent_version_id", existing_type=UUID_COL, nullable=False)
