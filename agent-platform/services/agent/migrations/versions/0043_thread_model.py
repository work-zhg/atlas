"""会话里选的模型（对话框的模型选择器）

Revision ID: 0043_thread_model
Revises: 0042_agent_draft
Create Date: 2026-11-11

thread.model  用户在输入框旁选的主模型，作用于之后的消息；NULL = 跟随智能体配置的模型。
              只对原生智能体的根会话生效（CLI 智能体要换模型去智能体配置里改，SubAgent 用自己的）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_thread_model"
down_revision: str | Sequence[str] | None = "0042_agent_draft"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("model", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("thread", "model")
