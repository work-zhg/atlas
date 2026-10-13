"""seed builtin `general` agent

Revision ID: 0002_builtin_agent
Revises: 0001_initial
Create Date: 2026-08-19

原型把"通用助手"定义为内置智能体：不可删除，未指定智能体的会话落到它。
它同时让 is_builtin 的 409 保护成为可测的真实路径。

spec 的形状必须与 server/schemas/agent.py 的 AgentSpecIn 一致 ——
tests/test_agents_api.py::test_builtin_agent_spec_is_loadable 会做往返校验。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import uuid

import sqlalchemy as sa
from alembic import op
from atlas_server.db.migration_types import JSON_COL, UUID_COL

revision: str = "0002_builtin_agent"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
BUILTIN_AGENT_ID = "00000000-0000-0000-0000-0000000000a1"
BUILTIN_VERSION_ID = "00000000-0000-0000-0000-0000000000a2"

# claude-sonnet-5：effort/thinking 留 auto，由 engine 按模型能力解析（§3 D3）
_SPEC = {
    "system_prompt": (
        "你是 Atlas 的通用助手。回答简洁、直接，不确定时明确说明。\n"
        "需要多步骤的任务先用 write_todos 拆解再执行。"
    ),
    "model": {
        "model": "claude-sonnet-5",
        "provider": "anthropic",
        "effort": None,
        "thinking": "auto",
        "max_output_tokens": 16384,
        "prompt_cache": True,
        "temperature": None,
    },
    "tool_names": ["write_todos", "filesystem"],
    "subagents": [],
    "limits": {
        "max_steps": 40,
        "timeout_s": 300,
        "max_total_tokens": 500000,
        "max_subagent_depth": 2,
        "tool_concurrency": 4,
        "require_approval_for": [],
    },
    "compaction": {
        "enabled": True,
        "trigger_ratio": 0.75,
        "target_ratio": 0.40,
        "keep_recent_turns": 3,
        "summarizer_model": "claude-haiku-4-5",
    },
}


def upgrade() -> None:
    """用 SQLAlchemy 表达式而不是手写 SQL。

    原来的写法里 `CAST(... AS uuid)`、`CAST(... AS jsonb)`、`ON CONFLICT`
    三样都是 PG 专有的。改成表达式之后，UUID 与 JSON 的存储形式交给
    UUID_COL / JSON_COL 去按方言处理，「已存在则跳过」改为先查后插 ——
    迁移是单次执行的，这点开销换来的是同一份脚本两个库都能跑。
    """
    conn = op.get_bind()
    agent = sa.table(
        "agent",
        sa.column("id", UUID_COL),
        sa.column("slug", sa.String),
        sa.column("name", sa.Text),
        sa.column("description", sa.Text),
        sa.column("avatar_key", sa.String),
        sa.column("status", sa.String),
        sa.column("is_builtin", sa.Boolean),
        sa.column("created_by", UUID_COL),
        sa.column("current_version_id", UUID_COL),
    )
    agent_version = sa.table(
        "agent_version",
        sa.column("id", UUID_COL),
        sa.column("agent_id", UUID_COL),
        sa.column("version", sa.Integer),
        sa.column("spec", JSON_COL),
        sa.column("created_by", UUID_COL),
    )

    aid = uuid.UUID(BUILTIN_AGENT_ID)
    vid = uuid.UUID(BUILTIN_VERSION_ID)
    uid = uuid.UUID(DEFAULT_USER_ID)

    if conn.execute(
        sa.select(sa.literal(1)).select_from(agent).where(agent.c.slug == "general")
    ).first() is None:
        op.bulk_insert(
            agent,
            [
                {
                    "id": aid,
                    "slug": "general",
                    "name": "通用助手",
                    "description": "系统默认智能体，不可删除。未指定智能体的会话会落到这里。",
                    "avatar_key": "general",
                    "status": "enabled",
                    "is_builtin": True,
                    "created_by": uid,
                }
            ],
        )

    if conn.execute(
        sa.select(sa.literal(1))
        .select_from(agent_version)
        .where(sa.and_(agent_version.c.agent_id == aid, agent_version.c.version == 1))
    ).first() is None:
        op.bulk_insert(
            agent_version,
            [{"id": vid, "agent_id": aid, "version": 1, "spec": _SPEC, "created_by": uid}],
        )

    conn.execute(sa.update(agent).where(agent.c.id == aid).values(current_version_id=vid))


def downgrade() -> None:
    conn = op.get_bind()
    agent = sa.table("agent", sa.column("id", UUID_COL), sa.column("current_version_id", UUID_COL))
    agent_version = sa.table("agent_version", sa.column("agent_id", UUID_COL))
    aid = uuid.UUID(BUILTIN_AGENT_ID)

    conn.execute(sa.update(agent).where(agent.c.id == aid).values(current_version_id=None))
    conn.execute(sa.delete(agent_version).where(agent_version.c.agent_id == aid))
    conn.execute(sa.delete(agent).where(agent.c.id == aid))
