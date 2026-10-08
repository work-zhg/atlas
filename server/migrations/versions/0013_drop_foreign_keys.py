"""删掉全部外键：引用完整性与级联删除改由代码逻辑负责

Revision ID: 0013_drop_foreign_keys
Revises: 0012_suspension_reasons
Create Date: 2026-10-04

约定：表上不建外键，只保留引用列与索引（db/models.py 模块说明）。

★ upgrade 不按名字删，而是读出每张表**实际存在**的外键逐个删 —— 不同环境的约束名
  可能不一致（naming convention 变过、手工建过），按名字删会漏，而这条约定要的是
  「一个都不剩」。
★ downgrade 按 0012 时的定义原样重建（含 ON DELETE CASCADE）。数据里若已有
  孤儿行（父行已删、子行还在），重建会失败 —— 那是去掉外键之后的预期状态，
  需要先清理孤儿再降级。

去掉外键后，原先由数据库代劳的级联删除不再发生：删会话不会再带走它的消息、run、
事件、文件记录、子会话；删 agent 不会再带走它的版本。这些由代码逻辑补上（本迁移
不处理）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_drop_foreign_keys"
down_revision: str | Sequence[str] | None = "0012_suspension_reasons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 0012 时的全部外键：(表, 约束名, 本表列, 引用表, 引用列, ondelete)
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str, str | None], ...] = (
    ("agent", "fk_agent_created_by_app_user", "created_by", "app_user", "id", None),
    ("agent", "fk_agent_current_version", "current_version_id", "agent_version", "id", None),
    ("agent_version", "fk_agent_version_agent_id_agent", "agent_id", "agent", "id", "CASCADE"),
    ("agent_version", "fk_agent_version_created_by_app_user", "created_by", "app_user", "id", None),
    ("thread", "fk_thread_agent_id_agent", "agent_id", "agent", "id", None),
    ("thread", "fk_thread_created_by_app_user", "created_by", "app_user", "id", None),
    ("thread", "fk_thread_parent", "parent_thread_id", "thread", "id", "CASCADE"),
    ("message", "fk_message_thread_id_thread", "thread_id", "thread", "id", "CASCADE"),
    (
        "run",
        "fk_run_agent_version_id_agent_version",
        "agent_version_id",
        "agent_version",
        "id",
        None,
    ),
    ("run", "fk_run_parent", "parent_run_id", "run", "id", None),
    ("run", "fk_run_thread_id_thread", "thread_id", "thread", "id", "CASCADE"),
    ("run_event", "fk_run_event_run_id_run", "run_id", "run", "id", "CASCADE"),
    ("run_event", "fk_run_event_thread", "thread_id", "thread", "id", "CASCADE"),
    ("run_file", "fk_run_file_run_id_run", "run_id", "run", "id", "CASCADE"),
    ("run_file", "fk_run_file_thread_id_thread", "thread_id", "thread", "id", "CASCADE"),
    ("approval", "fk_approval_decided_by_app_user", "decided_by", "app_user", "id", None),
    ("approval", "fk_approval_run_id_run", "run_id", "run", "id", "CASCADE"),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk.get("name"):
                op.drop_constraint(fk["name"], table, type_="foreignkey")


def downgrade() -> None:
    for table, name, column, ref_table, ref_column, ondelete in _FOREIGN_KEYS:
        op.create_foreign_key(name, table, ref_table, [column], [ref_column], ondelete=ondelete)
