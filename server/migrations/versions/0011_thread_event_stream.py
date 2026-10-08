"""事件流的订阅单位从 run 换成 thread

Revision ID: 0011_thread_event_stream
Revises: 0010_run_suspended
Create Date: 2026-09-23

前端原先只订阅**父 run 那一条**流，而子 run 的过程活在它自己的流上 ——
那条流谁也不订阅。父 run 在委派处挂起之后，父流再不产出任何事件，于是：

  · 用户盯着一个不动的界面，分不清「在等」和「挂了」
  · 子智能体请求审批时**弹窗永远不出现**，600s 后按拒绝处理
    （设计 detail/suspension.html §06）

根治办法是一个会话一条流：该会话下所有 run（含子 run）的事件都进去。
落到 schema 上就是把 `run_event` 的主键从 `(run_id, seq)` 换成
`(thread_id, thread_seq)`。

★ thread_id 是**根会话**的 id（Thread.stream_thread_id），不是 run 所属
  会话的 id。子 run 活在子会话上，它的事件要写进父会话那条流 —— 这一条
  是「一个会话看到所有事」的全部实现。

★ 为什么 TRUNCATE 而不是回填
  thread_seq 本可用 ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY
  ts, run_id, seq) 生成，但那要处理「回填期间有并发写入导致水位不准」。
  项目未上线，轨迹历史可弃 —— 风险降为零。
  downgrade 同样 TRUNCATE，两个方向语义对称：都不保留数据。

★ seq 列保留
  过渡期前端仍用 run 级 seq 做游标（S4 才切换），所以两个序号并存。
  切换后 seq 只剩诊断价值：「这个事件是那个 run 的第几个」。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import UUID_COL

revision: str = "0011_thread_event_stream"
down_revision: str | Sequence[str] | None = "0010_run_suspended"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ★ 主键约束的**真名**（查库确认：`pk_run_event`）。
#:
#:   写死并走 raw SQL，不用 `op.drop_constraint` —— naming_convention 会对
#:   传进去的名字再加一层前缀，而 0001 建表时给的名字本身可能已带一层。
#:   迁移 0010 正是这么撞上「约束 ck_run_status_enum 不存在」的
#:   （库里实际叫 ck_run_ck_run_status_enum）。
_PK = "pk_run_event"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _drop_pk() -> None:
    if _is_postgres():
        op.execute(f"ALTER TABLE run_event DROP CONSTRAINT {_PK}")
    else:
        op.execute("ALTER TABLE run_event DROP PRIMARY KEY")


def _add_pk(columns: str) -> None:
    if _is_postgres():
        op.execute(f"ALTER TABLE run_event ADD CONSTRAINT {_PK} PRIMARY KEY ({columns})")
    else:
        op.execute(f"ALTER TABLE run_event ADD PRIMARY KEY ({columns})")


def upgrade() -> None:
    # 轨迹历史可弃 —— 见文件头的论证。必须先清，否则新列的 NOT NULL
    # 无从填值。
    op.execute("TRUNCATE TABLE run_event")

    op.add_column("run_event", sa.Column("thread_id", UUID_COL, nullable=False))
    op.add_column("run_event", sa.Column("thread_seq", sa.Integer(), nullable=False))

    _drop_pk()
    _add_pk("thread_id, thread_seq")

    # 按 run 查轨迹仍要支持（单个 run 的详情页、诊断）
    op.create_index("ix_run_event_run", "run_event", ["run_id", "seq"])

    op.create_foreign_key(
        "fk_run_event_thread",
        "run_event",
        "thread",
        ["thread_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.execute("TRUNCATE TABLE run_event")

    op.drop_constraint("fk_run_event_thread", "run_event", type_="foreignkey")
    op.drop_index("ix_run_event_run", table_name="run_event")

    _drop_pk()
    _add_pk("run_id, seq")

    op.drop_column("run_event", "thread_seq")
    op.drop_column("run_event", "thread_id")
