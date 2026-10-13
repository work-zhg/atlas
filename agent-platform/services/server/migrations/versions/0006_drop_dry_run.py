"""移除试跑：run 必然属于一个会话、必然指向一份已保存的配置版本

Revision ID: 0006_drop_dry_run
Revises: 0005_thread_summary
Create Date: 2026-09-18

试跑（`POST /agents/{id}/dry-run`）整体下线。它在文件系统改造后的成本明显
升高：没有会话就没有 OSS 工作区前缀，要么造临时前缀（多一条生命周期要管），
要么为它单独保留一个 StateBackend 实现（多一个协议实现）—— 而它本来只是
编辑器里的一个便利功能。

本迁移基本是 0004 的 downgrade，外加一条 0001 遗留的可空收紧：

  0004 让 agent_version_id 可空 + 加 spec_snapshot / input_snapshot
  0001 让 thread_id 可空（NULL = dry-run）

两者一起收回之后，可复现性（§5.4「run 能回答当时用的哪份配置」）
从「靠 CHECK 约定」变成「靠两条非空外键」。

⚠️ 不可逆的数据删除
    DELETE FROM run WHERE agent_version_id IS NULL
  会级联带走这些 run 的 run_event（ON DELETE CASCADE）。
  生产库执行前先 SELECT count(*) 看清量级 —— 这批行的性质是
  「用户在编辑器里点过试跑」，没有会话归属，不在任何会话历史里出现。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import JSON_COL_NULLABLE, UUID_COL

revision: str = "0006_drop_dry_run"
down_revision: str | Sequence[str] | None = "0005_thread_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先清存量试跑 —— 它们的两个目标列都为 NULL，收紧非空之前必须走掉。
    # 顺序要紧：约束还在时删行是安全的，反过来会被 CHECK 挡住。
    op.execute("DELETE FROM run WHERE agent_version_id IS NULL OR thread_id IS NULL")

    op.drop_constraint("run_spec_source", "run", type_="check")
    op.drop_column("run", "input_snapshot")
    op.drop_column("run", "spec_snapshot")

    # ★ MySQL 的 alter_column 必须重述完整类型，否则会丢掉原类型定义。
    #   existing_type 在两种方言上都需要显式给出。
    op.alter_column("run", "agent_version_id", existing_type=UUID_COL, nullable=False)
    op.alter_column("run", "thread_id", existing_type=UUID_COL, nullable=False)


def downgrade() -> None:
    op.alter_column("run", "thread_id", existing_type=UUID_COL, nullable=True)
    op.alter_column("run", "agent_version_id", existing_type=UUID_COL, nullable=True)
    op.add_column("run", sa.Column("spec_snapshot", JSON_COL_NULLABLE, nullable=True))
    op.add_column("run", sa.Column("input_snapshot", JSON_COL_NULLABLE, nullable=True))
    op.create_check_constraint(
        "run_spec_source",
        "run",
        "(agent_version_id IS NOT NULL) <> (spec_snapshot IS NOT NULL)",
    )
