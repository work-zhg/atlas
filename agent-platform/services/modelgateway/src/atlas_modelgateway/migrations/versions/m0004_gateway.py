"""自研模型网关：租户、账号、线路、调用记录；模型加类型能力价格，Key 加摘要和限额；去掉 LiteLLM

Revision ID: m0004
Revises: m0003
Create Date: 2026-11-19

agent-platform/doc/design/model-gateway-design.html。

★ 多租户（tenant-isolation-design §8）：模型、账号、线路、Key 都由租户自己管。已有数据全部归原来的
  平台租户：环境变量 TENANT_BACKFILL_UUID。表里有数据却没给，迁移直接失败。
★ 已有模型的能力按「能调工具、能思考」回填，能不能看图沿用 supports_vision（它们现在就在跑工具）；
  上下文、最大输出、价格空着，由管理员补。
★ Key 的摘要由原文算出（sha256 十六进制），网关只按摘要认 Key。
★ gw_model_call 是按月的分区表，分区由网关启动时和每天建（本月和下个月）。
★ 不再同步 LiteLLM（认 Key、转发都由网关做）：LiteLLM 专用的列去掉；synced_models 改名
  effective_models（管理平台按授权算好的、这把 Key 此刻能用的模型）。LiteLLM 在过渡期只是一个账号
  （命令行 bootstrap-litellm）。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from atlas_tenancy.migration import add_tenant, drop_tenant, scope_unique, unscope_unique
from sqlalchemy.dialects import postgresql

revision: str = "m0004"
down_revision: str | Sequence[str] | None = "m0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TABLES = ("gw_model", "gw_api_key", "gw_audit")
_JSON = postgresql.JSONB()


def _backfill() -> UUID:
    raw = os.environ.get("TENANT_BACKFILL_UUID", "").strip()
    if raw:
        return UUID(raw)
    bind = op.get_bind()
    for table in OLD_TABLES:
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(
                f"表 {table} 里已有数据：设置 TENANT_BACKFILL_UUID 为原平台租户的 uuid 再迁移"
            )
    return UUID(int=0)


def _row_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False),
        sa.Column("tenant_uuid", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    tenant = _backfill()
    for table in OLD_TABLES:
        add_tenant(op, table, backfill=tenant)
    scope_unique(op, "gw_model", "uq_gw_model_name", ["name"], name="uq_gw_model_tenant_uuid_name")

    # ── 模型：类型、启用、能力、价格；供应商挪到账号上 ─────────────────
    op.add_column(
        "gw_model", sa.Column("kind", sa.String(16), nullable=False, server_default="chat")
    )
    op.add_column(
        "gw_model", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("gw_model", sa.Column("caps", _JSON, nullable=False, server_default="{}"))
    op.add_column("gw_model", sa.Column("price", _JSON, nullable=False, server_default="{}"))
    op.create_check_constraint("kind", "gw_model", "kind IN ('chat','embedding','rerank')")
    op.execute(
        "UPDATE gw_model SET caps = jsonb_build_object("
        "'kind', 'chat', 'vision', supports_vision, 'tools', true, 'thinking', 'optional')"
    )
    for column in ("supports_vision", "litellm_model_id", "provider"):
        op.drop_column("gw_model", column)

    # ── Key：摘要、末 4 位、用途名、限额 ────────────────────────────────
    op.add_column("gw_api_key", sa.Column("secret_digest", sa.String(64), nullable=True))
    op.add_column(
        "gw_api_key", sa.Column("secret_tail", sa.String(8), nullable=False, server_default="")
    )
    op.add_column(
        "gw_api_key", sa.Column("label", sa.String(100), nullable=False, server_default="")
    )
    op.add_column("gw_api_key", sa.Column("monthly_budget", sa.Numeric(18, 4), nullable=True))
    op.add_column("gw_api_key", sa.Column("rpm", sa.Integer(), nullable=True))
    op.add_column("gw_api_key", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    # 客户端 Key 重置后旧的再保留一会儿（正在跑的会话不断）
    op.add_column("gw_api_key", sa.Column("prev_digest", sa.String(64), nullable=True))
    op.add_column("gw_api_key", sa.Column("prev_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_gw_api_key_prev_digest", "gw_api_key", ["prev_digest"])
    op.alter_column("gw_api_key", "synced_models", new_column_name="effective_models")
    op.alter_column("gw_api_key", "secret", existing_type=sa.String(128), type_=sa.Text())
    op.add_column(
        "gw_api_key", sa.Column("block_reason", sa.String(200), nullable=False, server_default="")
    )
    # 用户 Key 一人可以多把（用途名区分）
    op.drop_index("uq_gw_api_key_user", table_name="gw_api_key")
    op.create_index(
        "ix_gw_api_key_user",
        "gw_api_key",
        ["user_uuid"],
        postgresql_where=sa.text("key_type = 'user'"),
    )
    for column in ("synced_blocked", "synced_at", "sync_error", "litellm_token"):
        op.drop_column("gw_api_key", column)
    op.execute(
        "UPDATE gw_api_key SET secret_digest = encode(sha256(convert_to(secret, 'UTF8')), 'hex'), "
        "secret_tail = right(secret, 4) WHERE secret IS NOT NULL"
    )
    op.create_index(
        "uq_gw_api_key_digest",
        "gw_api_key",
        ["secret_digest"],
        unique=True,
        postgresql_where=sa.text("secret_digest IS NOT NULL"),
    )
    op.drop_index("uq_gw_api_key_client", table_name="gw_api_key")
    op.create_index(
        "uq_gw_api_key_client",
        "gw_api_key",
        ["tenant_uuid", "client_type", "client_code"],
        unique=True,
        postgresql_where=sa.text("key_type = 'client'"),
    )

    # ── 账号 ────────────────────────────────────────────────────────────
    op.create_table(
        "gw_account",
        *_row_columns(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("endpoints", _JSON, nullable=False, server_default="{}"),
        sa.Column("secret_cipher", sa.Text(), nullable=True),
        sa.Column("secret_tail", sa.String(8), nullable=False, server_default=""),
        sa.Column("egress", sa.String(16), nullable=False, server_default="direct"),
        sa.Column("first_content_timeout_s", sa.Integer(), nullable=True),
        sa.Column("balance_alert", sa.Numeric(18, 4), nullable=True),
        sa.Column("remark", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("balance", sa.Numeric(18, 4), nullable=True),
        sa.Column("balance_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.CheckConstraint("egress IN ('direct','proxy')", name="ck_gw_account_egress"),
        sa.UniqueConstraint("uuid", name="uq_gw_account_uuid"),
        sa.UniqueConstraint("tenant_uuid", "name", name="uq_gw_account_tenant_uuid_name"),
    )
    op.create_index("ix_gw_account_tenant_uuid", "gw_account", ["tenant_uuid"])

    # ── 线路 ────────────────────────────────────────────────────────────
    op.create_table(
        "gw_route",
        *_row_columns(),
        sa.Column("model_uuid", sa.Uuid(), nullable=False),
        sa.Column("account_uuid", sa.Uuid(), nullable=False),
        sa.Column("upstream_model", sa.String(200), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("weight > 0", name="ck_gw_route_weight"),
        sa.UniqueConstraint("uuid", name="uq_gw_route_uuid"),
        sa.UniqueConstraint(
            "model_uuid", "account_uuid", "upstream_model", name="uq_gw_route_target"
        ),
    )
    op.create_index("ix_gw_route_tenant_uuid", "gw_route", ["tenant_uuid"])
    op.create_index("ix_gw_route_model_uuid", "gw_route", ["model_uuid"])
    op.create_index("ix_gw_route_account_uuid", "gw_route", ["account_uuid"])

    # ── 调用记录（按月分区） ────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE gw_model_call (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY,
            started_at TIMESTAMPTZ NOT NULL,
            tenant_uuid UUID NOT NULL,
            call_uuid UUID NOT NULL,
            key_uuid UUID,
            key_kind VARCHAR(16) NOT NULL DEFAULT '',
            model VARCHAR(128) NOT NULL,
            session_key VARCHAR(128),
            entry_format VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            error_category VARCHAR(32),
            attempts JSONB NOT NULL DEFAULT '[]',
            first_content_ms INTEGER,
            duration_ms INTEGER NOT NULL,
            input_tokens BIGINT NOT NULL DEFAULT 0,
            cache_write_tokens BIGINT NOT NULL DEFAULT 0,
            cache_hit_tokens BIGINT NOT NULL DEFAULT 0,
            output_tokens BIGINT NOT NULL DEFAULT 0,
            estimated BOOLEAN NOT NULL DEFAULT false,
            price JSONB NOT NULL DEFAULT '{}',
            cost NUMERIC(18, 6) NOT NULL DEFAULT 0,
            notes JSONB NOT NULL DEFAULT '{}',
            CONSTRAINT pk_gw_model_call PRIMARY KEY (id, started_at)
        ) PARTITION BY RANGE (started_at)
        """
    )
    op.create_index(
        "uq_gw_model_call_uuid", "gw_model_call", ["call_uuid", "started_at"], unique=True
    )
    op.create_index("ix_gw_model_call_tenant_uuid", "gw_model_call", ["tenant_uuid"])
    op.create_index("ix_gw_model_call_tenant_time", "gw_model_call", ["tenant_uuid", "started_at"])
    op.create_index(
        "ix_gw_model_call_key_time", "gw_model_call", ["tenant_uuid", "key_uuid", "started_at"]
    )
    op.create_index(
        "ix_gw_model_call_model_time", "gw_model_call", ["tenant_uuid", "model", "started_at"]
    )


def downgrade() -> None:
    op.execute("DROP TABLE gw_model_call CASCADE")
    op.drop_table("gw_route")
    op.drop_table("gw_account")
    op.drop_index("uq_gw_api_key_client", table_name="gw_api_key")
    op.create_index(
        "uq_gw_api_key_client",
        "gw_api_key",
        ["client_type", "client_code"],
        unique=True,
        postgresql_where=sa.text("key_type = 'client'"),
    )
    op.drop_index("uq_gw_api_key_digest", table_name="gw_api_key")
    op.drop_index("ix_gw_api_key_prev_digest", table_name="gw_api_key")
    op.drop_index("ix_gw_api_key_user", table_name="gw_api_key")
    op.create_index(
        "uq_gw_api_key_user",
        "gw_api_key",
        ["user_uuid"],
        unique=True,
        postgresql_where=sa.text("key_type = 'user'"),
    )
    op.drop_column("gw_api_key", "block_reason")
    op.alter_column("gw_api_key", "secret", existing_type=sa.Text(), type_=sa.String(128))
    op.alter_column("gw_api_key", "effective_models", new_column_name="synced_models")
    op.add_column("gw_api_key", sa.Column("synced_blocked", sa.Boolean(), nullable=True))
    op.add_column("gw_api_key", sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("gw_api_key", sa.Column("sync_error", sa.Text(), nullable=True))
    op.add_column("gw_api_key", sa.Column("litellm_token", sa.String(128), nullable=True))
    for column in (
        "prev_until",
        "prev_digest",
        "expires_at",
        "rpm",
        "monthly_budget",
        "label",
        "secret_tail",
        "secret_digest",
    ):
        op.drop_column("gw_api_key", column)
    op.drop_constraint("ck_gw_model_kind", "gw_model", type_="check")
    op.add_column(
        "gw_model", sa.Column("provider", sa.String(64), nullable=False, server_default="")
    )
    op.add_column("gw_model", sa.Column("litellm_model_id", sa.String(128), nullable=True))
    op.add_column(
        "gw_model",
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for column in ("price", "caps", "enabled", "kind"):
        op.drop_column("gw_model", column)
    unscope_unique(op, "gw_model", "uq_gw_model_tenant_uuid_name", "uq_gw_model_name", ["name"])
    for table in reversed(OLD_TABLES):
        drop_tenant(op, table)
