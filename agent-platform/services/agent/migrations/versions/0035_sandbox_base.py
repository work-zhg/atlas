"""沙箱基座 · 生命周期：sandbox_template、sandbox_instance、sandbox_event（改造计划 B2）

Revision ID: 0035_sandbox_base
Revises: 0034_approval_execution_modes
Create Date: 2026-11-04

c4-model/agent/sandbox/code-sandbox-base-lifecycle.html：

    sandbox_template   模板进库，按 code 管理；一个模板绑定一个 lifecycle server
    sandbox_instance   实例登记：Atlas 的 sandbox_uuid、OpenSandbox 的 id、模板版本、两个令牌
                       （原先令牌存在 Redis 的 atlas:{kind}:token:{id}，丢了所有沙箱都连不上）
    sandbox_event      生命周期日志，只追加；实例删了日志还在

★ 种子：三个模板（sbx-strict → sbx、cli → cli、bench → bench）的内容来自包里的
  sandbox/base/lifecycle/seed/*.yaml，也是 deploy/local/opensandbox.sh 首次部署时写 ConfigMap 的来源，
  所以记为已发布的第 1 版。已有集群上的 ConfigMap 若是旧内容，发布一次就对齐（drifted 能查出来）。
★ 存量沙箱不迁移（已定决策）：Redis 里的路由和令牌不读，基座对账上线后它们会被当作孤儿删掉。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import resources
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import JSON_COL, TS_COL, UUID_COL, now_default
from atlas_agent.ids import uuid7

revision: str = "0035_sandbox_base"
down_revision: str | Sequence[str] | None = "0034_approval_execution_modes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (code, server, 说明)
SEED = [
    ("sbx-strict", "sbx", "原生沙箱 strict 档：UID 1000、根只读、drop ALL、seccomp RuntimeDefault"),
    ("cli", "cli", "CLI 智能体的会话 Pod：UID 1000 只设在主容器上（egress sidecar 要 root 跑 nft）"),
    ("bench", "bench", "评测档：root、根可写，只给评测题镜像用"),
]
#: 种子由系统写入，不是某个人
SYSTEM = UUID(int=0)


def _seed_text(code: str) -> str:
    return (
        resources.files("atlas_agent.sandbox.base.lifecycle")
        .joinpath("seed", f"{code}.yaml")
        .read_text(encoding="utf-8")
    )


def upgrade() -> None:
    template = op.create_table(
        "sandbox_template",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("template_uuid", UUID_COL, nullable=False),
        sa.Column("code", sa.String(63), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("server", sa.String(63), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("published_sha", sa.String(64), nullable=True),
        sa.Column("published_at", TS_COL, nullable=True),
        sa.Column("updated_by_uuid", UUID_COL, nullable=False),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_template"),
        sa.UniqueConstraint("template_uuid", name="uk_sandbox_template_uuid"),
        sa.UniqueConstraint("code", name="uk_sandbox_template_code"),
        sa.UniqueConstraint("server", name="uk_sandbox_template_server"),
    )
    op.create_table(
        "sandbox_instance",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sandbox_uuid", UUID_COL, nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("template_uuid", UUID_COL, nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="Pending"),
        sa.Column("observed_at", TS_COL, nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("service_token", sa.Text(), nullable=True),
        sa.Column("created_at", TS_COL, nullable=False, server_default=now_default()),
        sa.Column("updated_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_instance"),
        sa.UniqueConstraint("sandbox_uuid", name="uk_sandbox_instance_uuid"),
        # NULL 不参与唯一：还没回填 id 的登记可以有多个
        sa.UniqueConstraint("external_id", name="uk_sandbox_instance_external"),
    )
    op.create_index("ix_sandbox_instance_template", "sandbox_instance", ["template_uuid"])
    op.create_table(
        "sandbox_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_uuid", UUID_COL, nullable=False),
        sa.Column("sandbox_uuid", UUID_COL, nullable=True),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("detail", JSON_COL, nullable=False),
        sa.Column("occurred_at", TS_COL, nullable=False, server_default=now_default()),
        sa.PrimaryKeyConstraint("id", name="pk_sandbox_event"),
        sa.UniqueConstraint("event_uuid", name="uk_sandbox_event_uuid"),
    )
    op.create_index("ix_sandbox_event_sandbox", "sandbox_event", ["sandbox_uuid", "occurred_at"])
    op.create_index("ix_sandbox_event_external", "sandbox_event", ["external_id"])
    op.create_index("ix_sandbox_event_time", "sandbox_event", ["occurred_at"])

    now = datetime.now(UTC)
    rows = []
    for code, server, description in SEED:
        text = _seed_text(code)
        rows.append(
            {
                "template_uuid": uuid7(),
                "code": code,
                "description": description,
                "server": server,
                "version": 1,
                "yaml": text,
                "published_version": 1,
                "published_sha": hashlib.sha256(text.encode()).hexdigest(),
                "published_at": now,
                "updated_by_uuid": SYSTEM,
            }
        )
    op.bulk_insert(template, rows)


def downgrade() -> None:
    op.drop_table("sandbox_event")
    op.drop_table("sandbox_instance")
    op.drop_table("sandbox_template")
