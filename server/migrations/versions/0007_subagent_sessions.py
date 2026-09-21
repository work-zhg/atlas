"""子智能体持有自己的会话：thread 自引用 + run 的父子关系

Revision ID: 0007_subagent_sessions
Revises: 0006_drop_dry_run
Create Date: 2026-09-18

「子会话 = thread，不是 run」这个决定落到 schema 上就是本迁移。

`thread` 本来就是「一段有历史的对话」：message 序列、摘要边界、压缩计数、
标题都在。子智能体需要的正是这些 —— 于是子会话的 run 走与普通 run
**完全同一条**执行链路，压缩 / 审批 / 取消 / 孤儿回收一行新代码都不用写。

三列 + 一条唯一索引：

    parent_thread_id     NULL = 用户会话；非空 = 某个会话的子智能体会话
    subagent_name        与 parent 组成恢复的查找键
    external_session_id  ACP 侧的会话标识，留给 loadSession（acp 详设那一期）

★ 唯一键为什么是 (parent_thread_id, subagent_name)
  「下次再委派到这个子智能体时恢复」—— 恢复的依据就是哪个父会话 + 哪个
  子智能体。两者都是已有的东西，不需要发明新标识。

★ 为什么是部分唯一索引（subagent_name IS NOT NULL AND status = 'active'）
  用户会话两列都为 NULL。多行 NULL 在 PG 的普通唯一索引里互不冲突，但
  MySQL 的行为并不在所有版本/引擎上都一致，而「第二个用户会话建不出来」
  是灾难性的。
  条件里的 status='active' 是另一半：一次性（ephemeral）子会话以
  status='ephemeral' 出生，因此不占这个键 —— 同一个一次性子智能体可以
  真并行（设计 §09），而持久子智能体的同名委派仍被唯一约束按住。
  MySQL 没有部分索引：那边退化成普通复合索引，唯一性由
  SubagentService 的 SELECT-then-INSERT 保证（子会话的创建被父 run 串行化）。

★ ondelete="CASCADE" 的方向
  删父会话时子会话一起走 —— 子会话脱离父会话没有意义。代价是它会连带删掉
  子会话的 message / run / run_event，删一个会话的影响面因此变大了，
  界面上的删除确认要显示子会话数量。

`run.parent_run_id` 不做 CASCADE：子 run 是独立可寻址的执行记录，父 run 的
行没了不该让子 run 的用量统计跟着消失。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import UUID_COL

revision: str = "0007_subagent_sessions"
down_revision: str | Sequence[str] | None = "0006_drop_dry_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column("thread", sa.Column("parent_thread_id", UUID_COL, nullable=True))
    op.add_column("thread", sa.Column("subagent_name", sa.String(64), nullable=True))
    op.add_column("thread", sa.Column("external_session_id", sa.String(128), nullable=True))
    op.create_foreign_key(
        "fk_thread_parent",
        "thread",
        "thread",
        ["parent_thread_id"],
        ["id"],
        ondelete="CASCADE",
    )

    if _is_postgres():
        op.create_index(
            "ux_thread_subagent",
            "thread",
            ["parent_thread_id", "subagent_name"],
            unique=True,
            postgresql_where=sa.text("subagent_name IS NOT NULL AND status = 'active'"),
        )
    else:
        # MySQL 无部分索引：普通复合索引保住查找性能，唯一性由
        # SubagentService._resolve_thread 的 SELECT-then-INSERT 保证
        # （子会话的创建被父 run 串行化，窗口极窄）。
        op.create_index("ix_thread_subagent", "thread", ["parent_thread_id", "subagent_name"])

    op.add_column("run", sa.Column("parent_run_id", UUID_COL, nullable=True))
    op.create_foreign_key("fk_run_parent", "run", "run", ["parent_run_id"], ["id"])
    # 准入控制要数「本 run 累计委派了几次」，见 config.subagent_max_per_run
    op.create_index("ix_run_parent", "run", ["parent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_parent", table_name="run")
    op.drop_constraint("fk_run_parent", "run", type_="foreignkey")
    op.drop_column("run", "parent_run_id")

    if _is_postgres():
        op.drop_index("ux_thread_subagent", table_name="thread")
    else:
        op.drop_index("ix_thread_subagent", table_name="thread")
    op.drop_constraint("fk_thread_parent", "thread", type_="foreignkey")
    op.drop_column("thread", "external_session_id")
    op.drop_column("thread", "subagent_name")
    op.drop_column("thread", "parent_thread_id")
