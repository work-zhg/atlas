"""运营中心的入口与运维命令。

python -m atlas_opscenter serve                 起 HTTP 服务（默认 :8100）
python -m atlas_opscenter migrate               升级运营库
python -m atlas_opscenter bootstrap --account ops --name 运营管理员
                                                首次部署：建第一个运营管理员（临时密码只输出这一次）
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


def bootstrap(args: argparse.Namespace) -> int:
    from .accounts import Accounts
    from .db.session import get_engine, get_sessionmaker

    async def _go() -> str:
        try:
            async with get_sessionmaker()() as session:
                temp = await Accounts(session, get_settings()).bootstrap(args.account, args.name)
                await session.commit()
                return temp
        finally:
            await get_engine().dispose()

    temp = asyncio.run(_go())
    print(f"已创建运营管理员 {args.account}")
    print(f"临时密码（只显示这一次，首次登录须修改）：{temp}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_opscenter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--account", required=True)
    boot.add_argument("--name", required=True)
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
        if args.command == "bootstrap":
            return bootstrap(args)
    except OpsError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
