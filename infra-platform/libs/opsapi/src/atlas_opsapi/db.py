"""两张表（建在宿主自己的库里，表名前缀 app_，和应用配置库、定时任务库一致）。

    app_tenant_state   一个租户在本系统里的状态。没有行 = ACTIVE；
                       PURGED 的行保留，晚到的请求照样被拒
    app_ops_request    运营请求记录：按幂等键去重；也是本系统一侧的运营记录
                       （谁、何时、对哪个租户、做了什么）

★ 两张表都不是租户表（不继承 TenantOwned）：本来就在系统范围里读写。
★ 列类型沿用应用配置库的跨方言定义（PG 上 JSONB、UUID 原生；MySQL 上 JSON、CHAR(32)）。
"""

from __future__ import annotations

import sqlalchemy as sa
from atlas_appconfig.db import JSONType, UTCDateTime, UUIDType

__all__ = ["metadata", "request_table", "state_table"]

metadata = sa.MetaData()

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

state_table = sa.Table(
    "app_tenant_state",
    metadata,
    sa.Column("id", _ID, primary_key=True, autoincrement=True),
    sa.Column("tenant_uuid", UUIDType, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("operation_uuid", UUIDType, nullable=True),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("changed_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("tenant_uuid", name="uk_app_tenant_state_tenant"),
)

request_table = sa.Table(
    "app_ops_request",
    metadata,
    sa.Column("id", _ID, primary_key=True, autoincrement=True),
    sa.Column("idempotency_key", UUIDType, nullable=True),
    sa.Column("action", sa.String(64), nullable=False),
    sa.Column("tenant_uuid", UUIDType, nullable=True),
    sa.Column("operator_uuid", UUIDType, nullable=True),
    sa.Column("operator_name", sa.String(64), nullable=True),
    sa.Column("operation_uuid", UUIDType, nullable=True),
    sa.Column("result", JSONType, nullable=True),
    sa.Column("created_at", UTCDateTime(), nullable=False),
    sa.UniqueConstraint("idempotency_key", name="uk_app_ops_request_key"),
    sa.Index("ix_app_ops_request_tenant", "tenant_uuid", "created_at"),
)
