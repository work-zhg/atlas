"""运营中心的入口与运维命令。

python -m atlas_opscenter serve                 起 HTTP 服务（默认 :8100）
python -m atlas_opscenter migrate               升级运营库
python -m atlas_opscenter sync-catalog          把自己的应用目录同步进用户中心、给系统租户开通
                                                （服务里也定时做）

★ 运营人员不在这里建：先在用户中心建系统租户（python -m atlas_usercenter bootstrap-system），
  系统租户的管理员在用户中心里开账号、授运营模块。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .errors import OpsError
from .settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_OPS_PORT：K8s Service 会注入同名遗留变量（同用户中心的坑）。"""
    raw = os.environ.get("ATLAS_OPS_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8100


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=os.environ.get("ATLAS_OPS_HOST", "0.0.0.0"), port=_port())


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)


def sync_catalog() -> int:
    """手动同步一次自己的应用目录（运营服务启动后也会定时做）：部署后想马上登录时用。"""
    from .access import CatalogSync, ModuleCatalog
    from .gateway import OpsClient, SystemRegistry

    settings = get_settings()
    registry = SystemRegistry(settings.systems)

    async def _go() -> None:
        client = OpsClient(registry, settings)
        try:
            await CatalogSync(client, ModuleCatalog(registry), settings.home_url).run()
        finally:
            await client.aclose()

    asyncio.run(_go())
    print("已同步运营中心的应用目录，并给系统租户开通")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_opscenter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    sub.add_parser("sync-catalog")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "migrate":
            migrate()
            return 0
        if args.command == "sync-catalog":
            return sync_catalog()
    except OpsError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
