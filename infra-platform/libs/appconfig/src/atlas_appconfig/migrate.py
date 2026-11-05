"""建表：库自带 alembic 迁移，宿主在自己的迁移步骤里调一次。

    async with engine.begin() as conn:
        await conn.run_sync(atlas_appconfig.upgrade)

★ 版本表是独立的 app_config_alembic_version：库升级表结构时宿主再跑一次即可，和宿主自己的
  迁移互不影响。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

__all__ = ["downgrade", "upgrade"]

_SCRIPTS = Path(__file__).parent / "migrations"


def _config(connection: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPTS))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(connection: Connection, revision: str = "head") -> None:
    command.upgrade(_config(connection), revision)


def downgrade(connection: Connection, revision: str = "base") -> None:
    command.downgrade(_config(connection), revision)
