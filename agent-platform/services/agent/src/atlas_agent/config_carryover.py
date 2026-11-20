"""多租户之后几个运行期配置换了名（作用域登记后不能改，只能换 key 名），把旧值搬到新键。

    sandbox.quota_per_user  → capacity.sandboxes_per_person 的系统值（每人上限成了租户设置）
    workspace.quota_mb      → capacity.storage_mb_per_person 的系统值
    platform.default_agent  → tenant.default_agent 在原平台租户里的值（TENANT_BACKFILL_UUID）

由 `python -m atlas_agent migrate-appconfig` 在应用配置库升级之后调（必须在它之后：旧的表还没有
作用域和租户列）。幂等：新键已经有值就不动；新键的行已由启动登记建好、还是空值的，填上旧值并
留一条修订。旧键的行留在库里不删（代码不再声明，管理接口不再列出）。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from atlas_appconfig.db import SYSTEM_ROW
from atlas_appconfig.db import config_table as C
from atlas_appconfig.db import revision_table as R
from sqlalchemy.engine import Connection

__all__ = ["carry_over"]

logger = logging.getLogger(__name__)

#: (旧 key, 新 key, 值的类型, 搬到原平台租户而不是系统值)
MOVES = (
    ("sandbox.quota_per_user", "capacity.sandboxes_per_person", "int", False),
    ("workspace.quota_mb", "capacity.storage_mb_per_person", "int", False),
    ("platform.default_agent", "tenant.default_agent", "string", True),
)


def _row(conn: Connection, key: str, tenant: UUID) -> Any:
    return conn.execute(sa.select(C).where(C.c.key == key, C.c.tenant_uuid == tenant)).first()


def carry_over(conn: Connection) -> int:
    """→ 搬了几条。"""
    backfill = os.environ.get("TENANT_BACKFILL_UUID", "").strip()
    moved = 0
    for old, new, kind, to_tenant in MOVES:
        source = _row(conn, old, SYSTEM_ROW)
        if source is None or source.value is None:
            continue
        if to_tenant and not backfill:
            logger.warning("%s 有值，但没设 TENANT_BACKFILL_UUID：不知道搬给哪个租户，跳过", old)
            continue
        owner = UUID(backfill) if to_tenant else SYSTEM_ROW
        now = datetime.now(UTC)
        note = f"多租户：从 {old} 搬来"
        target = _row(conn, new, owner)
        if target is None:
            config_uuid = uuid4()
            conn.execute(
                sa.insert(C).values(
                    config_uuid=config_uuid,
                    key=new,
                    scope="tenant",
                    tenant_uuid=owner,
                    value_kind=kind,
                    element_kind=None,
                    value=source.value,
                    version=1,
                    description="",
                    updated_by="migration",
                    created_at=now,
                    updated_at=now,
                )
            )
            version = 1
        elif target.value is None and target.version == 1:
            config_uuid, version = target.config_uuid, 2
            conn.execute(
                sa.update(C)
                .where(C.c.config_uuid == config_uuid, C.c.version == 1)
                .values(value=source.value, version=2, updated_by="migration", updated_at=now)
            )
        else:
            continue  # 新键已经有人改过：以它为准
        conn.execute(
            sa.insert(R).values(
                revision_uuid=uuid4(),
                config_uuid=config_uuid,
                version=version,
                value=source.value,
                note=note,
                changed_by="migration",
                created_at=now,
            )
        )
        moved += 1
        logger.info("运行期配置 %s → %s：%r", old, new, source.value)
    return moved
