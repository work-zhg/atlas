"""用户中心的入口与运维命令。

python -m atlas_usercenter serve        起 HTTP 服务（默认 :8030）
python -m atlas_usercenter migrate      升级用户中心库并同步内置应用
python -m atlas_usercenter bootstrap --company 星海科技 --account admin --name 管理员 --email …
                                        首次部署：建根部门与首个超级管理员（临时密码只输出这一次）
python -m atlas_usercenter bootstrap-system --code platform --company 平台运营 \
        --account admin --name … --email …
                                        建系统租户（运营团队，只能有一个；运营中心只能开给它）
python -m atlas_usercenter jwt-keygen   生成一把访问令牌的签名私钥
                                        （放进部署 Secret：ATLAS_UC_JWT_SIGNING_KEY）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .db.session import get_engine, get_sessionmaker
from .errors import UCError
from .settings import get_settings

logger = logging.getLogger("atlas_usercenter")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_UC_PORT 一类名字：k8s Service 会注入同名遗留变量（cluster 踩过的坑）。"""
    raw = os.environ.get("ATLAS_UC_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8030


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(
        create_app(cors_origins=get_settings().cors_origins),
        host=os.environ.get("ATLAS_UC_HOST", "0.0.0.0"),
        port=_port(),
    )


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)

    from .builtin import sync_builtin

    async def _sync() -> None:
        try:
            async with get_sessionmaker()() as session:
                await sync_builtin(session)
                await session.commit()
        finally:
            await get_engine().dispose()

    asyncio.run(_sync())


def bootstrap(args: argparse.Namespace) -> int:
    from .bootstrap import bootstrap as run

    async def _go() -> tuple[str, str]:
        try:
            async with get_sessionmaker()() as session:
                result = await run(
                    session,
                    company=args.company,
                    account=args.account,
                    name=args.name,
                    email=args.email,
                )
                await session.commit()
                return result
        finally:
            await get_engine().dispose()

    account, temp = asyncio.run(_go())
    print(f"已创建根部门「{args.company}」与超级管理员 {account}")
    print(f"临时密码（只显示这一次，首次登录须修改）：{temp}")
    return 0


def bootstrap_system(args: argparse.Namespace) -> int:
    from .bootstrap import bootstrap_system as run

    async def _go() -> tuple[str, str]:
        try:
            async with get_sessionmaker()() as session:
                result = await run(
                    session,
                    code=args.code,
                    company=args.company,
                    account=args.account,
                    name=args.name,
                    email=args.email,
                )
                await session.commit()
                return result
        finally:
            await get_engine().dispose()

    account, temp = asyncio.run(_go())
    print(f"已创建系统租户 {args.code}「{args.company}」与超级管理员 {account}")
    print(f"临时密码（只显示这一次，首次登录须修改）：{temp}")
    print("下一步：运营中心启动后会给系统租户开通自己；在用户中心里给运营人员授运营模块")
    return 0


def jwt_keygen() -> int:
    from .auth.tokens import new_signing_key

    kid, private, public = new_signing_key()
    print(f"# kid：{kid}")
    print("# 私钥 → 部署 Secret 的 ATLAS_UC_JWT_SIGNING_KEY（只在用户中心里）：")
    print(private, end="")
    print("# 公钥 → 换钥时先放进旧实例的 ATLAS_UC_JWT_PUBLISHED_KEYS：")
    print(public, end="")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_usercenter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    sub.add_parser("jwt-keygen")
    system = sub.add_parser("bootstrap-system")
    system.add_argument("--code", required=True, help="企业代码（登录运营中心时填）")
    system.add_argument("--company", required=True, help="租户名称，如 平台运营")
    system.add_argument("--account", required=True)
    system.add_argument("--name", required=True)
    system.add_argument("--email", required=True)
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--company", required=True, help="根部门（公司）名称")
    boot.add_argument("--account", required=True)
    boot.add_argument("--name", required=True)
    boot.add_argument("--email", required=True)
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
        if args.command == "jwt-keygen":
            return jwt_keygen()
        if args.command == "bootstrap-system":
            return bootstrap_system(args)
    except UCError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
