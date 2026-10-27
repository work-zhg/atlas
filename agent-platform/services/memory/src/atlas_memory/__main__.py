"""记忆服务的入口与运维命令。

    python -m atlas_memory serve                     起 HTTP 服务（默认 :8060）
    python -m atlas_memory migrate                   升级记忆库到最新
    python -m atlas_memory scope list
    python -m atlas_memory scope add <编码> --key uid [--key project] [--description …]
                                     [--check-permission] [--data-code …] [--if-missing]
    python -m atlas_memory scope set <编码> [--description …]
                                     [--check-permission | --no-check-permission]
    python -m atlas_memory register-uc --account admin
                                     把应用、操作码、各作用域的数据编码登记到用户中心

★ Atlas 需要的作用域（记忆设计 §4.1，本期只用「用户」）。部署清单（deploy/local/36-memory.yaml）
  在迁移之后用 --if-missing 建好它；数据编码要管理员再跑一次 register-uc 登记到用户中心：
    python -m atlas_memory scope add user --key uid --description 用户 \
        --check-permission --if-missing
    python -m atlas_memory register-uc --account admin
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .db.session import get_engine, get_sessionmaker
from .errors import MemoryServiceError
from .scopes import ScopeService, scope_view
from .settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_MEMORY_PORT：Service 名叫 atlas-memory 时 kubelet 会注入
    ATLAS_MEMORY_PORT=tcp://… 这种遗留变量（atlas-config 踩过同一个坑）。"""
    raw = os.environ.get("ATLAS_MEMORY_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8060


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=os.environ.get("ATLAS_MEMORY_HOST", "0.0.0.0"), port=_port())


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)


async def _with_scopes(fn: Callable[[ScopeService], Awaitable[Any]]) -> Any:
    try:
        async with get_sessionmaker()() as session:
            result = await fn(ScopeService(session))
            await session.commit()
            return result
    finally:
        await get_engine().dispose()


def _print_scope(view: dict[str, Any]) -> None:
    check = f"数据权限 {view['data_code']}" if view["check_permission"] else "不校验数据权限"
    print(
        f"{view['code']:<16} 字段={','.join(view['key_fields']):<16} {check}  {view['description']}"
    )


def scope_command(args: argparse.Namespace) -> int:
    if args.action == "list":
        rows = asyncio.run(_with_scopes(lambda svc: _views(svc)))
        for view in rows:
            _print_scope(view)
        return 0
    if args.action == "add":

        async def _add(svc: ScopeService) -> dict[str, Any] | None:
            if args.if_missing and await svc.find(args.code) is not None:
                return None  # 已有就不动：作用域归管理员管，部署脚本不覆盖它的设置
            row = await svc.create(
                code=args.code,
                description=args.description or "",
                key_fields=args.key,
                check_permission=args.check_permission,
                data_code=args.data_code,
                actor=args.actor or getpass.getuser(),
            )
            return scope_view(row)

        view = asyncio.run(_with_scopes(_add))
        if view is None:
            print(f"作用域 {args.code} 已存在，未改动")
            return 0
        _print_scope(view)
        if view["check_permission"]:
            print("  打开了数据权限开关：运行 register-uc 把数据编码登记到用户中心")
        return 0
    if args.action == "set":

        async def _set(svc: ScopeService) -> dict[str, Any]:
            row = await svc.update(
                args.code,
                description=args.description,
                check_permission=args.check_permission,
                data_code=args.data_code,
            )
            return scope_view(row)

        _print_scope(asyncio.run(_with_scopes(_set)))
        return 0
    raise AssertionError(args.action)


async def _views(svc: ScopeService) -> list[dict[str, Any]]:
    return [scope_view(s) for s in await svc.list()]


def register_command(args: argparse.Namespace) -> int:
    from .register import register

    views = asyncio.run(_with_scopes(_views))
    data_types = [
        {
            "code": v["data_code"],
            "name": f"记忆 · {v['description'] or v['code']}",
            "description": "一个作用域实例一条：只读（列目录、加载）/ 读写（写入、拆分、撤销）/ "
            "Owner（彻底删除）",
        }
        for v in views
        if v["check_permission"] and v["data_code"]
    ]
    settings = get_settings()
    password = os.environ.get("UC_ADMIN_PASSWORD") or getpass.getpass(
        f"用户中心账号 {args.account} 的密码："
    )
    register(
        args.uc_url or settings.uc_base_url,
        args.account,
        password,
        app_code=settings.uc_app_code,
        data_types=data_types,
        tenant=args.tenant,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_memory")
    parser.add_argument("--actor", help="操作人（默认当前系统用户）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")

    scope = sub.add_parser("scope").add_subparsers(dest="action", required=True)
    scope.add_parser("list")
    add = scope.add_parser("add")
    add.add_argument("code")
    add.add_argument("--key", action="append", required=True, help="关联主键字段，可重复，有序")
    add.add_argument("--description")
    add.add_argument("--check-permission", action="store_true")
    add.add_argument("--data-code")
    add.add_argument("--if-missing", action="store_true", help="已存在就什么也不做（部署用）")
    upd = scope.add_parser("set")
    upd.add_argument("code")
    upd.add_argument("--description")
    upd.add_argument("--check-permission", action=argparse.BooleanOptionalAction, default=None)
    upd.add_argument("--data-code")

    reg = sub.add_parser("register-uc", help="把应用、操作码、数据编码登记到用户中心")
    reg.add_argument("--account", required=True, help="用户中心管理员账号")
    reg.add_argument("--uc-url", default=None)
    reg.add_argument("--tenant", default=None, help="管理员所在的企业代码（有多个租户时必填）")
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
        if args.command == "scope":
            return scope_command(args)
        if args.command == "register-uc":
            return register_command(args)
    except MemoryServiceError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
