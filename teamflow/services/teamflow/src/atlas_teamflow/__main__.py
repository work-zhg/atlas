"""AI TeamFlow 的入口与运维命令。

python -m atlas_teamflow serve      起 HTTP 服务（默认 :8040；内嵌投递器和通知器）
python -m atlas_teamflow migrate    升级 TeamFlow 库（本服务的表 + 运营接口库的表）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .errors import TFError
from .settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_TF_PORT 一类名字：k8s Service 会注入同名遗留变量。"""
    raw = os.environ.get("ATLAS_TF_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8040


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=os.environ.get("ATLAS_TF_HOST", "0.0.0.0"), port=_port())


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)
    asyncio.run(_upgrade_opsapi())


async def _upgrade_opsapi() -> None:
    from atlas_opsapi import upgrade
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(upgrade)
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_teamflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "migrate":
            migrate()
            return 0
    except TFError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
