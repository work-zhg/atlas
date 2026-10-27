"""模型网关的初始表（doc/model-design.html）。

Revision ID: m0001
Revises:
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "m0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_PRICE = sa.Numeric(18, 6)


def _row() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "gw_model",
        *_row(),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.String(64), nullable=False, server_default=""),
        sa.Column("tags", _JSON, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("upstream_kind", sa.String(16), nullable=False, server_default="higress"),
        sa.Column("upstream_model", sa.String(128), nullable=False),
        sa.Column("protocols", _JSON, nullable=False),
        sa.Column("input_modalities", _JSON, nullable=False),
        sa.Column("capabilities", _JSON, nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("max_batch", sa.Integer(), nullable=True),
        sa.Column("data_policy", _JSON, nullable=False),
        sa.Column("capacity", _JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="onboarding"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disabled_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("deprecated_at", _TS, nullable=True),
        sa.Column("retire_at", _TS, nullable=True),
        sa.Column("retired_at", _TS, nullable=True),
        sa.Column("replacement", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("access_checked_at", _TS, nullable=True),
        sa.Column("access_check_ok", sa.Boolean(), nullable=True),
        sa.Column("access_check_detail", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('chat','embedding','rerank')", name="kind"),
        sa.CheckConstraint("upstream_kind IN ('higress')", name="upstream_kind"),
        sa.CheckConstraint(
            "status IN ('onboarding','available','deprecated','retired')", name="status"
        ),
    )

    op.create_table(
        "gw_model_price",
        *_row(),
        sa.Column("model_uuid", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("input", _PRICE, nullable=False),
        sa.Column("output", _PRICE, nullable=False),
        sa.Column("cache_read", _PRICE, nullable=True),
        sa.Column("cache_write", _PRICE, nullable=True),
        sa.Column("per_request", _PRICE, nullable=True),
        sa.Column("tiers", _JSON, nullable=False),
        sa.Column("effective_from", _TS, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_gw_model_price_model_from", "gw_model_price", ["model_uuid", "effective_from"]
    )

    op.create_table(
        "gw_model_probe",
        *_row(),
        sa.Column("model_uuid", sa.Uuid(), nullable=False),
        sa.Column("results", _JSON, nullable=False),
        sa.Column("suggested", _JSON, nullable=False),
        sa.Column("differences", _JSON, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("applied_by", sa.String(64), nullable=True),
        sa.Column("applied_at", _TS, nullable=True),
    )
    op.create_index("ix_gw_model_probe_model_at", "gw_model_probe", ["model_uuid", "created_at"])

    op.create_table(
        "gw_purpose",
        *_row(),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("default_model", sa.String(64), nullable=True),
        sa.Column("allowed_models", _JSON, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
    )

    op.create_table(
        "gw_model_key",
        *_row(),
        sa.Column("subject_uid", sa.Uuid(), nullable=False, unique=True),
        sa.Column("subject_type", sa.String(16), nullable=False, server_default="human"),
        sa.Column("subject_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("api_key", sa.String(80), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("issued_by", sa.String(64), nullable=False),
        sa.Column("rotated_at", _TS, nullable=False),
        sa.Column("disabled_at", _TS, nullable=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="status"),
    )

    op.create_table(
        "gw_model_call",
        *_row(),
        sa.Column("model", sa.String(64), nullable=False, server_default=""),
        sa.Column("upstream_model", sa.String(128), nullable=True),
        sa.Column("endpoint", sa.String(16), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subject_uid", sa.Uuid(), nullable=True),
        sa.Column("dept_id", sa.String(64), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=True),
        sa.Column("caller_app", sa.String(64), nullable=True),
        sa.Column("user_uid", sa.String(64), nullable=True),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("forwarded", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_reported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("price_uuid", sa.Uuid(), nullable=True),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_gw_model_call_at", "gw_model_call", ["created_at"])
    op.create_index("ix_gw_model_call_model_at", "gw_model_call", ["model", "created_at"])
    op.create_index("ix_gw_model_call_subject_at", "gw_model_call", ["subject_uid", "created_at"])
    op.create_index("ix_gw_model_call_purpose_at", "gw_model_call", ["purpose", "created_at"])

    op.create_table(
        "gw_budget",
        *_row(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
        sa.Column("starts_at", _TS, nullable=False),
        sa.Column("warn_ratio", sa.Float(), nullable=False, server_default="0.2"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
    )

    op.create_table(
        "gw_audit",
        *_row(),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("detail", _JSON, nullable=False),
    )
    op.create_index("ix_gw_audit_target_at", "gw_audit", ["target", "created_at"])


def downgrade() -> None:
    for table in (
        "gw_audit",
        "gw_budget",
        "gw_model_call",
        "gw_model_key",
        "gw_purpose",
        "gw_model_probe",
        "gw_model_price",
        "gw_model",
    ):
        op.drop_table(table)
