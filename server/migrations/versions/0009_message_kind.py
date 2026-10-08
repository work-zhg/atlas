"""message.kind：把「工具结果」从「对话轮」里分出来

Revision ID: 0009_message_kind
Revises: 0008_deepseek_models
Create Date: 2026-09-23

一轮的中间消息（模型发出的 tool_use、每个工具的结果）从此也要落库 ——
分段执行时段与段之间的唯一载体就是这张表（见 domain/messages.py）。

落到 schema 上只有一列。**role 枚举一个字都不用改**：Anthropic 的
tool_result 本来就是 user 角色的 content block，tool_use 是 assistant 的
content block，两者都装得进现有的 `content` JSON。

★ 那为什么还需要 kind
  工具结果也是 role='user'。而「最后一条 user 消息是本轮输入」是执行器
  重建任务时的判据（executor/inprocess.py::_prepare）—— 不加区分，一批
  工具结果就会被当成用户的新提问。表现是模型莫名回答一段 JSON，而用户
  真正的问题被归进历史。

★ 为什么不加索引
  查询总是先按 thread_id 收窄（已有 ix_message_thread），一个会话内的
  消息量是几百条量级，kind 只是其中的过滤条件。为它单独建索引是在给
  写入加成本换一个量不到的收益。

★ 历史数据
  已有行全部是对话轮，server_default='chat' 正确覆盖。这张表在此之前
  根本没有工具结果 —— 那正是本次改造要补的东西。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_message_kind"
down_revision: str | Sequence[str] | None = "0008_deepseek_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message",
        sa.Column("kind", sa.String(16), nullable=False, server_default="chat"),
    )
    op.create_check_constraint(
        "kind_enum",
        "message",
        "kind IN ('chat','tool_result')",
    )


def downgrade() -> None:
    op.drop_constraint("kind_enum", "message", type_="check")
    op.drop_column("message", "kind")
