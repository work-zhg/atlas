"""沙箱浏览器：登录态 browser_login、浏览器宿主 browser_host

Revision ID: 0046_browser
Revises: 0045_device
Create Date: 2026-11-14

开了浏览器能力的智能体在沙箱里用一个常驻的 Chromium，人接管登录后由平台保管登录态
（c4-model/agent/browser/design-browser.html）。

    browser_login   一个人在一个站点上的登录态：只有密文（AES-256-GCM，附加数据绑人和站点），
                    按 captured_at 只用更新的覆盖
    browser_host    哪些沙箱是浏览器宿主：退役前凭它判断要不要导出登录态

★ 接管是审批的第四种（approval.kind = 'takeover'）：kind 列 String(8) 放得下，也没有取值约束，
  审批表不用改。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import TS_COL, UUID_COL, now_default

revision: str = "0046_browser"
down_revision: str | Sequence[str] | None = "0045_device"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_login",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("login_uuid", UUID_COL, nullable=False),
        sa.Column("user_uuid", UUID_COL, nullable=False),
        sa.Column("site", sa.String(253), nullable=False),
        sa.Column("sealed", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.SmallInteger(), nullable=False),
        sa.Column("captured_at", TS_COL, nullable=False),
        sa.Column("captured_from", UUID_COL, nullable=True),
        sa.Column("expires_at", TS_COL, nullable=True),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_browser_login"),
        sa.UniqueConstraint("login_uuid", name="uk_browser_login_uuid"),
        sa.UniqueConstraint("user_uuid", "site", name="uk_browser_login_site"),
    )
    op.create_table(
        "browser_host",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("host_uuid", UUID_COL, nullable=False),
        sa.Column("sandbox_uuid", UUID_COL, nullable=False),
        sa.Column("user_uuid", UUID_COL, nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("started_at", TS_COL, nullable=False),
        sa.Column("exported_at", TS_COL, nullable=True),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_browser_host"),
        sa.UniqueConstraint("host_uuid", name="uk_browser_host_uuid"),
        sa.UniqueConstraint("sandbox_uuid", name="uk_browser_host_sandbox"),
        sa.CheckConstraint("kind IN ('native','cli')", name="browser_host_kind_enum"),
    )
    op.create_index("ix_browser_host_user", "browser_host", ["user_uuid"])


def downgrade() -> None:
    op.drop_index("ix_browser_host_user", table_name="browser_host")
    op.drop_table("browser_host")
    op.drop_table("browser_login")
