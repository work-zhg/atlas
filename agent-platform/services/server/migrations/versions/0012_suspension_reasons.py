"""审批也走挂起：run.waiting_on + approval.tool_call_id

Revision ID: 0012_suspension_reasons
Revises: 0011_thread_event_stream
Create Date: 2026-09-23

审批原先是「在线阻塞等 600s」：一个 asyncio.Task 全程挂着，进程重启即丢，
而 600s 对小时级任务根本不够 —— 一个跑 40 分钟的 CLI 在第 35 分钟弹审批，
用户没盯着屏幕就超时按拒绝处理。

改成与委派同一套挂起机制之后（detail/suspension.html §06），需要两列：

★ run.waiting_on —— 「这个 run 在等什么」
  没有它，`_resume_if_ready` 的 barrier 会判错方向。委派的 barrier 是「子 run
  终态」，而审批挂起时**没有子 run** —— barrier 立刻满足，于是续跑、又挂起、
  再续跑，**死循环**。两种 reason 的等待条件不同，必须显式记下来。

  形如 [{"reason": "approval", "token": "<approval_id>"}]。

  ★ 委派挂起原先靠「哨兵字符串 + parent_run_id 查 barrier」就够，刻意没有
    建表（那一次反悔记在 §12 修正记录 1）。审批加入后 barrier 判据分叉，
    这一列因此回来了 —— 但它是一列，不是一张表，且理由不同。

★ approval.tool_call_id —— 审计用
  approval_id 现在由 `uuid5(NS, "{run_id}:{tool_call_id}")` 派生（必须可复现，
  否则续跑时算出新 id、查到 pending、再挂起一次 → 死循环）。把来源那个
  tool_call_id 一起存下来，排查「这个审批对应哪次调用」时不必反推。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import JSON_COL

revision: str = "0012_suspension_reasons"
down_revision: str | Sequence[str] | None = "0011_thread_event_stream"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("waiting_on", JSON_COL, nullable=True))
    op.add_column("approval", sa.Column("tool_call_id", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("approval", "tool_call_id")
    op.drop_column("run", "waiting_on")
