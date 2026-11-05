"""库自带的迁移：版本表是独立的 app_config_alembic_version，不与宿主的迁移混在一起。

由 atlas_appconfig.upgrade(connection) 调起，连接由宿主给（宿主的迁移步骤里调一次）。
"""

from __future__ import annotations

from alembic import context
from atlas_appconfig.db import metadata

VERSION_TABLE = "app_config_alembic_version"

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, version_table=VERSION_TABLE)
with context.begin_transaction():
    context.run_migrations()
