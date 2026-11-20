"""运营库：运营账号、登录会话、运营审计、租户操作与步骤

Revision ID: o0001_initial
Revises:
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "o0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)


def _id(table: str) -> list[sa.SchemaItem]:
    return [
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def upgrade() -> None:
    op.create_table(
        "ops_operator",
        *_id("ops_operator"),
        sa.Column("operator_uuid", sa.Uuid, nullable=False),
        sa.Column("account", sa.String(32), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("must_change_password", sa.Boolean, nullable=False),
        sa.Column("failed_logins", sa.Integer, nullable=False),
        sa.Column("locked_until", _TS, nullable=True),
        sa.Column("last_login_at", _TS, nullable=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.UniqueConstraint("operator_uuid", name="uq_ops_operator_operator_uuid"),
        sa.UniqueConstraint("account", name="uq_ops_operator_account"),
        sa.CheckConstraint("role IN ('ADMIN','OBSERVER')", name="ck_ops_operator_role_enum"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_ops_operator_status_enum"),
    )
    op.create_table(
        "ops_session",
        *_id("ops_session"),
        sa.Column("session_uuid", sa.Uuid, nullable=False),
        sa.Column("operator_uuid", sa.Uuid, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.UniqueConstraint("session_uuid", name="uq_ops_session_session_uuid"),
        sa.UniqueConstraint("token_hash", name="uq_ops_session_token_hash"),
    )
    op.create_index("ix_ops_session_operator_uuid", "ops_session", ["operator_uuid"])
    op.create_table(
        "ops_audit",
        *_id("ops_audit"),
        sa.Column("audit_uuid", sa.Uuid, nullable=False),
        sa.Column("at", _TS, nullable=False),
        sa.Column("operator_uuid", sa.Uuid, nullable=True),
        sa.Column("operator_name", sa.String(32), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(200), nullable=True),
        sa.Column("tenant_uuid", sa.Uuid, nullable=True),
        sa.Column("detail", _JSON, nullable=True),
        sa.Column("result", sa.String(16), nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
        sa.UniqueConstraint("audit_uuid", name="uq_ops_audit_audit_uuid"),
    )
    op.create_index("ix_ops_audit_at", "ops_audit", [sa.text("at DESC")])
    op.create_index("ix_ops_audit_tenant", "ops_audit", ["tenant_uuid", sa.text("at DESC")])
    op.create_table(
        "ops_tenant_operation",
        *_id("ops_tenant_operation"),
        sa.Column("operation_uuid", sa.Uuid, nullable=False),
        sa.Column("tenant_uuid", sa.Uuid, nullable=False),
        sa.Column("tenant_code", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("params", _JSON, nullable=True),
        sa.Column("requested_by", sa.Uuid, nullable=True),
        sa.Column("requested_by_name", sa.String(32), nullable=True),
        sa.Column("audit_uuid", sa.Uuid, nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_until", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        sa.UniqueConstraint("operation_uuid", name="uq_ops_tenant_operation_operation_uuid"),
        sa.CheckConstraint(
            "kind IN ('OPEN','SUSPEND','RESUME','CLOSE')", name="ck_ops_tenant_operation_kind_enum"
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','DONE')", name="ck_ops_tenant_operation_status_enum"
        ),
    )
    op.create_index("ix_ops_tenant_operation_tenant_uuid", "ops_tenant_operation", ["tenant_uuid"])
    # 同一个租户同一时间只有一个进行中的操作
    op.create_index(
        "uq_ops_tenant_operation_running",
        "ops_tenant_operation",
        ["tenant_uuid"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "ops_operation_step",
        *_id("ops_operation_step"),
        sa.Column("step_uuid", sa.Uuid, nullable=False),
        sa.Column("operation_uuid", sa.Uuid, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("system", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", _TS, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("rejected", sa.Boolean, nullable=False),
        sa.Column("result", _JSON, nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
        sa.UniqueConstraint("step_uuid", name="uq_ops_operation_step_step_uuid"),
        sa.CheckConstraint(
            "status IN ('PENDING','DONE','FAILED')", name="ck_ops_operation_step_status_enum"
        ),
    )
    op.create_index(
        "ix_ops_operation_step_operation_uuid", "ops_operation_step", ["operation_uuid"]
    )
    op.create_index(
        "ix_ops_operation_step_due", "ops_operation_step", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    for table in (
        "ops_operation_step",
        "ops_tenant_operation",
        "ops_audit",
        "ops_session",
        "ops_operator",
    ):
        op.drop_table(table)
