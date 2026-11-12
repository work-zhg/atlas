"""会话的工作目录：thread.workspace_dir

Revision ID: 0044_thread_workspace_dir
Revises: 0043_thread_model
Create Date: 2026-11-12

用户的文件改放 {user}/files/（工作空间页面的根）。会话的工作目录相对它，建会话时定下：
指定了就用指定的，没指定就是 {日期}/{会话 id}；子会话复制父会话的（domain/workspace_layout.py）。

    thread.workspace_dir   工作目录（相对 files/），之后不变
    ix_thread_workspace_dir  按人 + 工作目录前缀查「哪些会话在用这个目录」

★ 已有的会话补成 {建会话的日期}/{根会话 id}。旧布局 {user}/{根会话}/workspace/ 里的文件不搬：
  用户定的是直接删掉（python -m atlas_agent purge-legacy-workspaces），这些会话之后从空目录开始。
★ 已有的沙箱一律标成 retiring（同 0039）：会话 Pod 里的 bridge 是按旧的 cwd 拉起的，不能接着用。
  迁移只改库，Pod 由维护者的通用供给对账删掉；下一次确保时分配新的。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_thread_workspace_dir"
down_revision: str | Sequence[str] | None = "0043_thread_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500


def upgrade() -> None:
    op.add_column("thread", sa.Column("workspace_dir", sa.String(512), nullable=True))
    bind = op.get_bind()
    # 根会话：{建会话的日期}/{会话 id}。日期在 Python 里算（各数据库的日期格式化函数不一样）
    rows = bind.execute(
        sa.text("SELECT id, thread_uuid, created_at FROM thread WHERE root_thread_uuid IS NULL")
    ).all()
    dirs = {str(r.thread_uuid): f"{_day(r.created_at)}/{r.thread_uuid}" for r in rows}
    params = [{"id": r.id, "dir": dirs[str(r.thread_uuid)]} for r in rows]
    # 子会话：根会话的（根会话已经被删、只剩孤儿子会话时，用它自己的）
    children = bind.execute(
        sa.text(
            "SELECT id, thread_uuid, root_thread_uuid, created_at FROM thread "
            "WHERE root_thread_uuid IS NOT NULL"
        )
    ).all()
    params += [
        {
            "id": r.id,
            "dir": dirs.get(str(r.root_thread_uuid)) or f"{_day(r.created_at)}/{r.thread_uuid}",
        }
        for r in children
    ]
    update = sa.text("UPDATE thread SET workspace_dir = :dir WHERE id = :id")
    for i in range(0, len(params), _BATCH):
        bind.execute(update, params[i : i + _BATCH])
    op.alter_column("thread", "workspace_dir", existing_type=sa.String(512), nullable=False)
    op.create_index("ix_thread_workspace_dir", "thread", ["created_by_uuid", "workspace_dir"])
    op.execute(sa.text("UPDATE user_sandbox SET status = 'retiring'"))


def downgrade() -> None:
    op.drop_index("ix_thread_workspace_dir", table_name="thread")
    op.drop_column("thread", "workspace_dir")


def _day(created_at: object) -> str:
    """created_at → YYYY-MM-DD（PG 给 datetime，SQLite 等给字符串）。"""
    return created_at.date().isoformat() if hasattr(created_at, "date") else str(created_at)[:10]
