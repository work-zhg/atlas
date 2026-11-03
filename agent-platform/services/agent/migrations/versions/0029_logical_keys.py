"""主键约定：自增 id + xxx_uuid 逻辑主键（改造计划 A0）

Revision ID: 0029_logical_keys
Revises: 0028_run_state
Create Date: 2026-11-03

和用户中心、记忆服务、TeamFlow 同一约定：id 是自增主键，只在库内使用，不对外、不做关联；
xxx_uuid 是逻辑主键（UUIDv7，Python 侧生成），表间引用和 API 都用它。

uuid 主键的 9 张表   原 id 改名为 xxx_uuid（值不变）并建唯一约束 uk_<表>_uuid；
                     新增自增 id 作主键，按 created_at 的先后编号 —— message 的列表按
                     (created_at, id) 排，同一时刻的两条消息不再由随机 UUID 定先后
自然键的 8 张表      新增自增 id 作主键，原主键改为唯一约束；run_event 补 event_uuid
引用列               xxx_id 改名为 xxx_uuid，created_by / decided_by / updated_by 改为 xxx_by_uuid

★ 现有索引随列改名，名字不变（PG、MySQL 都是）。
★ MySQL 分支没有在 MySQL 上实跑过（开发机只有 PG）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from atlas_agent.db.migration_types import UUID_COL, dialect

revision: str = "0029_logical_keys"
down_revision: str | Sequence[str] | None = "0028_run_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 引用列改名：表 → [(旧名, 新名)]
RENAMES: dict[str, list[tuple[str, str]]] = {
    "agent": [("current_version_id", "current_version_uuid"), ("created_by", "created_by_uuid")],
    "agent_version": [("agent_id", "agent_uuid"), ("created_by", "created_by_uuid")],
    "thread": [
        ("agent_id", "agent_uuid"),
        ("parent_thread_id", "parent_thread_uuid"),
        ("root_thread_id", "root_thread_uuid"),
        ("created_by", "created_by_uuid"),
    ],
    "message": [("thread_id", "thread_uuid"), ("run_id", "run_uuid")],
    "run": [
        ("thread_id", "thread_uuid"),
        ("agent_version_id", "agent_version_uuid"),
        ("parent_run_id", "parent_run_uuid"),
        ("root_run_id", "root_run_uuid"),
        ("created_by", "created_by_uuid"),
    ],
    "run_event": [("thread_id", "thread_uuid"), ("run_id", "run_uuid")],
    "run_state": [("run_id", "run_uuid")],
    "run_file": [("thread_id", "thread_uuid"), ("run_id", "run_uuid")],
    "approval": [("run_id", "run_uuid"), ("decided_by", "decided_by_uuid")],
    "skill_usage_daily": [("agent_id", "agent_uuid")],
    "thread_memory": [("thread_id", "thread_uuid")],
    "memory_user_setting": [("user_id", "user_uuid")],
    "memory_backfill": [("thread_id", "thread_uuid"), ("user_id", "user_uuid")],
    "service_model": [("updated_by", "updated_by_uuid")],
    "instruction": [("updated_by", "updated_by_uuid")],
}

#: uuid 主键的表：(表, 逻辑主键列)。原 id 列改名为它，新 id 按 created_at 编号
UUID_TABLES: list[tuple[str, str]] = [
    ("app_user", "user_uuid"),
    ("agent", "agent_uuid"),
    ("agent_version", "agent_version_uuid"),
    ("thread", "thread_uuid"),
    ("message", "message_uuid"),
    ("run", "run_uuid"),
    ("run_file", "run_file_uuid"),
    ("approval", "approval_uuid"),
    ("memory_backfill", "memory_backfill_uuid"),
]

#: 自然键的表：(表, 原主键列（改名后）, 唯一约束名, 编号顺序)
KEYED_TABLES: list[tuple[str, list[str], str, str]] = [
    ("model_catalog", ["model"], "uk_model_catalog_model", "model"),
    ("run_event", ["thread_uuid", "thread_seq"], "uk_run_event_stream", "ts, thread_seq"),
    ("run_state", ["run_uuid", "key"], "uk_run_state_key", "updated_at"),
    (
        "skill_usage_daily",
        ["day", "slug", "version", "agent_uuid"],
        "uk_skill_usage_daily_key",
        "day, slug",
    ),
    ("thread_memory", ["thread_uuid"], "uk_thread_memory_thread", "updated_at"),
    ("memory_user_setting", ["user_uuid"], "uk_memory_user_setting_user", "updated_at"),
    ("service_model", ["purpose"], "uk_service_model_purpose", "purpose"),
    ("instruction", ["scope", "owner_id"], "uk_instruction_owner", "updated_at"),
]


def _mysql() -> bool:
    return dialect() in ("mysql", "mariadb")


def _q(name: str) -> str:
    """列名加引号：key 在 MySQL 是保留字。"""
    return f"`{name}`" if _mysql() else f'"{name}"'


def _rename(table: str, old: str, new: str) -> None:
    # PG 与 MySQL 8 都支持 RENAME COLUMN，不必给出列类型
    op.execute(f"ALTER TABLE {table} RENAME COLUMN {_q(old)} TO {_q(new)}")


def _add_serial_pk(table: str, keys: list[str], order_by: str) -> None:
    """新增自增 id 作主键：先按 order_by 给存量行编号，再挂上自增和主键。

    keys 是能唯一定位一行的列（MySQL 的 UPDATE … JOIN 按它们对上）。
    """
    if _mysql():
        op.execute(f"ALTER TABLE {table} ADD COLUMN id BIGINT NULL FIRST")
        cols = ", ".join(_q(k) for k in keys)
        on = " AND ".join(f"t.{_q(k)} = s.{_q(k)}" for k in keys)
        op.execute(
            f"UPDATE {table} t JOIN (SELECT {cols}, ROW_NUMBER() OVER (ORDER BY {order_by}) AS rn "
            f"FROM {table}) s ON {on} SET t.id = s.rn"
        )
        # AUTO_INCREMENT 的计数器从现有最大值往后接
        op.execute(
            f"ALTER TABLE {table} MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT, "
            f"ADD CONSTRAINT pk_{table} PRIMARY KEY (id)"
        )
        return
    op.execute(f"ALTER TABLE {table} ADD COLUMN id BIGINT")
    op.execute(
        f"UPDATE {table} SET id = s.rn FROM (SELECT ctid AS row_ref, "
        f"row_number() OVER (ORDER BY {order_by}) AS rn FROM {table}) s "
        f"WHERE {table}.ctid = s.row_ref"
    )
    # 和 BIGSERIAL 建出来的一样：序列归属于列，删列时一起删
    op.execute(f"CREATE SEQUENCE {table}_id_seq OWNED BY {table}.id")
    op.execute(
        f"SELECT setval('{table}_id_seq', COALESCE((SELECT max(id) FROM {table}), 0) + 1, false)"
    )
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{table}_id_seq'), "
        f"ALTER COLUMN id SET NOT NULL"
    )
    op.create_primary_key(f"pk_{table}", table, ["id"])


def upgrade() -> None:
    for table, pairs in RENAMES.items():
        for old, new in pairs:
            _rename(table, old, new)

    for table, key in UUID_TABLES:
        op.drop_constraint(f"pk_{table}", table, type_="primary")
        _rename(table, "id", key)
        op.create_unique_constraint(f"uk_{table}_uuid", table, [key])
        _add_serial_pk(table, [key], f"created_at, {_q(key)}")

    for table, keys, unique, order_by in KEYED_TABLES:
        op.drop_constraint(f"pk_{table}", table, type_="primary")
        op.create_unique_constraint(unique, table, keys)
        _add_serial_pk(table, keys, order_by)

    op.add_column("run_event", sa.Column("event_uuid", UUID_COL, nullable=True))
    if _mysql():
        # sa.Uuid 在 MySQL 上存成 32 位十六进制的 CHAR(32)
        op.execute("UPDATE run_event SET event_uuid = REPLACE(UUID(), '-', '')")
    else:
        op.execute("UPDATE run_event SET event_uuid = gen_random_uuid()")
    op.alter_column("run_event", "event_uuid", existing_type=UUID_COL, nullable=False)
    op.create_unique_constraint("uk_run_event_uuid", "run_event", ["event_uuid"])


def downgrade() -> None:
    op.drop_constraint("uk_run_event_uuid", "run_event", type_="unique")
    op.drop_column("run_event", "event_uuid")

    # 删掉 id 列，主键约束（和 PG 的序列）跟着没了
    for table, keys, unique, _ in reversed(KEYED_TABLES):
        op.drop_column(table, "id")
        op.drop_constraint(unique, table, type_="unique")
        op.create_primary_key(f"pk_{table}", table, keys)

    for table, key in reversed(UUID_TABLES):
        op.drop_column(table, "id")
        op.drop_constraint(f"uk_{table}_uuid", table, type_="unique")
        _rename(table, key, "id")
        op.create_primary_key(f"pk_{table}", table, ["id"])

    for table, pairs in RENAMES.items():
        for old, new in pairs:
            _rename(table, new, old)
