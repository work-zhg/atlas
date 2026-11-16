"""模型标注能不能看图（gw_model.supports_vision）。

Atlas 据此决定开了浏览器的智能体给不给 computer use（看截图、用系统键鼠操作网页）：
没标注的按不能（design-browser-device.html 原理 2）。

Revision ID: m0003
Revises: m0002
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m0003"
down_revision: str | Sequence[str] | None = "m0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gw_model",
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("gw_model", "supports_vision")
