"""MCP 网关的入口与运维命令。

    python -m atlas_mcpgateway serve                     起 HTTP 服务（默认 :8070）
    python -m atlas_mcpgateway migrate                   升级网关库到最新
    python -m atlas_mcpgateway register-uc --account admin
                                     把应用、操作码、角色、数据编码 McpServer 登记到用户中心
    python -m atlas_mcpgateway service list
    python -m atlas_mcpgateway service add <服务名> --owner <用户中心 uid>
                                     [--upstream <Higress 上的名字>] [--display-name …]
                                     [--no-review] [--call-timeout 秒]
    python -m atlas_mcpgateway service refresh <服务名>
    python -m atlas_mcpgateway review <服务名> <工具名> approve|reject --actor <uid> [--comment …]
    python -m atlas_mcpgateway key issue <用户中心 uid> --actor <uid>
    python -m atlas_mcpgateway key issue-missing --actor <uid> [--service …]
                                     给有授权但没有 Key 的人批量发 Key
    python -m atlas_mcpgateway grant <服务名> --actor <uid>
                                     (--all-users | --user <uid> | --dept <uid>) [--owner]
                                     给人 / 部门授「使用」（--owner 授所有者）

  迁移（MCP 方案设计 §12.4，可以重复执行）：
    python -m atlas_mcpgateway import --owner-account admin --from-env '<MCP_SERVERS 的 JSON>'
                                     把 Atlas 原来经 MCP_SERVERS 接入的服务登记进网关

★ 命令行和管理接口调同一个 Registry / KeyStore：规则只有一份实现。管理端页面做出来之前，
  部署时用命令行登记服务、发 Key。
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
from uuid import UUID

from .auth import use_outbound
from .db.session import get_engine, get_sessionmaker
from .errors import GatewayError
from .keys import KeyStore, mask
from .perms import UCPermissions
from .registry import Registry
from .settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_MCPGATEWAY_PORT：Service 名叫 atlas-mcpgateway 时 kubelet 会注入
    ATLAS_MCPGATEWAY_PORT=tcp://… 这种遗留变量（atlas-config 踩过同一个坑）。"""
    raw = os.environ.get("ATLAS_MCPGW_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8070


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=os.environ.get("ATLAS_MCPGW_HOST", "0.0.0.0"), port=_port())


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)


async def _with_session(fn: Callable[[Any], Awaitable[Any]]) -> Any:
    try:
        async with get_sessionmaker()() as session:
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await get_engine().dispose()


def _print_service(row: Any) -> None:
    state = "启用" if row.status == "enabled" else "停用"
    check = {True: "接入检查通过", False: "⚠ 可以直连", None: "接入检查未完成"}[row.access_check_ok]
    fetched = f"拉取失败：{row.fetch_error}" if row.fetch_error else "定义已拉取"
    print(f"{row.name:<16} {state}  上游 {row.upstream_name:<16} {check}  {fetched}")


def service_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.action == "list":

        async def _list(session: Any) -> None:
            registry = Registry(session, settings)
            for row in await registry.list():
                _print_service(row)
                for view in await registry.tools(row):
                    print(f"    {view.row.name:<32} {view.status:<16} {view.row.digest[:12]}")

        asyncio.run(_with_session(_list))
        return 0
    if args.action == "add":

        async def _add(session: Any) -> Any:
            use_outbound({"X-Uid": args.owner, "X-Auth-Uid": args.owner})
            perms = UCPermissions(args.uc_url or settings.uc_base_url)
            try:
                return await Registry(session, settings).create(
                    perms,
                    name=args.name,
                    display_name=args.display_name or args.name,
                    description=args.description or "",
                    upstream_name=args.upstream or args.name,
                    review_required=args.review,
                    call_timeout_s=args.call_timeout,
                    credential_mode="subject_key",
                    actor=args.owner,
                )
            finally:
                await perms.aclose()

        _print_service(asyncio.run(_with_session(_add)))
        return 0
    if args.action == "refresh":

        async def _refresh(session: Any) -> Any:
            registry = Registry(session, settings)
            return await registry.refresh(await registry.get(args.name))

        _print_service(asyncio.run(_with_session(_refresh)))
        return 0
    raise AssertionError(args.action)


def review_command(args: argparse.Namespace) -> int:
    async def _review(session: Any) -> Any:
        registry = Registry(session, get_settings())
        view = await registry.tool(await registry.get(args.service), args.tool)
        if view is None:
            raise GatewayError(f"MCP 服务 {args.service} 上没有工具 {args.tool}")
        return await registry.review(
            args.service,
            args.tool,
            digest=view.row.digest,
            decision="approved" if args.decision == "approve" else "rejected",
            comment=args.comment or "",
            actor=args.actor,
        )

    view = asyncio.run(_with_session(_review))
    print(f"{args.service}/{args.tool}  {view.status}  {view.row.digest[:12]}")
    return 0


def key_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.action == "issue-missing":
        from .migrate import issue_missing

        async def _missing(session: Any) -> Any:
            use_outbound({"X-Uid": args.actor, "X-Auth-Uid": args.actor})
            perms = UCPermissions(settings.uc_base_url)
            try:
                names = args.service or [
                    r.name
                    for r in await Registry(session, settings).list()
                    if r.status == "enabled"
                ]
                return await issue_missing(session, perms, names, actor=args.actor)
            finally:
                await perms.aclose()

        issued = asyncio.run(_with_session(_missing))
        for uid, name in issued:
            print(f"✓ 已发放 {name or uid}")
        print(f"共 {len(issued)} 把")
        return 0

    async def _issue(session: Any) -> Any:
        use_outbound({"X-Uid": args.actor, "X-Auth-Uid": args.actor})
        perms = UCPermissions(settings.uc_base_url)
        try:
            user = await perms.user(args.subject)
        finally:
            await perms.aclose()
        if user is None:
            raise GatewayError("用户中心里没有这个用户")
        return await KeyStore(session).issue(
            UUID(args.subject),
            subject_type=str(user.get("user_type") or "human"),
            subject_name=str(user.get("name") or ""),
            actor=args.actor,
        )

    row = asyncio.run(_with_session(_issue))
    print(f"✓ 已发放 {row.subject_name or row.subject_uid} 的 API Key {mask(row.api_key)}")
    return 0


async def _uid(perms: UCPermissions, uid: str | None, account: str | None) -> str:
    """命令行里的人：直接给 uid，或给用户中心账号（部署脚本只知道账号）。"""
    if uid:
        return uid
    assert account is not None
    user = await perms.user_by_account(account)
    if user is None:
        raise GatewayError(f"用户中心里没有账号 {account}")
    return str(user["id"])


def _add_actor(parser: argparse.ArgumentParser, help_text: str = "操作人") -> None:
    actor = parser.add_mutually_exclusive_group(required=True)
    actor.add_argument("--actor", help=f"{help_text}（用户中心 uid）")
    actor.add_argument("--actor-account", help=f"{help_text}的用户中心账号")


def import_command(args: argparse.Namespace) -> int:
    import json

    from .migrate import import_services, legacy_from_env

    settings = get_settings()

    async def _import(session: Any) -> Any:
        perms = UCPermissions(settings.uc_base_url)
        try:
            owner = await _uid(perms, args.owner, args.owner_account)
            use_outbound({"X-Uid": owner, "X-Auth-Uid": owner})
            servers = legacy_from_env(json.loads(args.from_env))
            return await import_services(session, settings, perms, servers, owner=owner)
        finally:
            await perms.aclose()

    report = asyncio.run(_with_session(_import))
    for name in report.imported:
        print(f"✓ 已登记 {name}")
    for name in report.existing:
        print(f"· {name} 已经登记过")
    for name, why in report.skipped:
        print(f"✗ 跳过 {name}：{why}")
    return 1 if report.skipped else 0


def grant_command(args: argparse.Namespace) -> int:
    from .migrate import grant_all_users, root_dept
    from .perms import Grant

    settings = get_settings()

    async def _grant(session: Any) -> None:
        await Registry(session, settings).get(args.service)
        perms = UCPermissions(settings.uc_base_url)
        level = "OWNER" if args.owner else "READ"
        try:
            actor = await _uid(perms, args.actor, args.actor_account)
            use_outbound({"X-Uid": actor, "X-Auth-Uid": actor})
            if args.all_users:
                root = await root_dept(settings.uc_base_url)
                await grant_all_users(perms, args.service, root=root, operator=actor)
            elif args.user:
                await perms.grant(args.service, actor, [Grant(level, "user", args.user)])
            else:
                await perms.grant(
                    args.service, actor, [Grant(level, "dept", args.dept, include_sub=True)]
                )
        finally:
            await perms.aclose()

    asyncio.run(_with_session(_grant))
    print(f"✓ 已授权 {args.service}")
    return 0


def register_command(args: argparse.Namespace) -> int:
    from .register import register

    settings = get_settings()
    password = os.environ.get("UC_ADMIN_PASSWORD") or getpass.getpass(
        f"用户中心账号 {args.account} 的密码："
    )
    register(
        args.uc_url or settings.uc_base_url,
        args.account,
        password,
        app_code=settings.uc_app_code,
        tenant=args.tenant,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_mcpgateway")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")

    svc = sub.add_parser("service").add_subparsers(dest="action", required=True)
    svc.add_parser("list")
    add = svc.add_parser("add")
    add.add_argument("name")
    add.add_argument("--owner", required=True, help="注册人（用户中心 uid），成为服务所有者")
    add.add_argument("--upstream", help="Higress 上 MCP 服务的名字（缺省 = 服务名）")
    add.add_argument("--display-name")
    add.add_argument("--description")
    add.add_argument("--no-review", dest="review", action="store_false", help="不要求复核")
    add.add_argument("--call-timeout", type=float, default=None)
    add.add_argument("--uc-url", default=None)
    ref = svc.add_parser("refresh")
    ref.add_argument("name")

    rev = sub.add_parser("review", help="复核当前这一版定义")
    rev.add_argument("service")
    rev.add_argument("tool")
    rev.add_argument("decision", choices=["approve", "reject"])
    rev.add_argument("--actor", required=True)
    rev.add_argument("--comment")

    key = sub.add_parser("key").add_subparsers(dest="action", required=True)
    issue = key.add_parser("issue")
    issue.add_argument("subject", help="持有人（用户中心 uid，人或数字员工）")
    issue.add_argument("--actor", required=True)
    missing = key.add_parser("issue-missing", help="给有授权但没有 Key 的人批量发 Key")
    _add_actor(missing)
    missing.add_argument(
        "--service", action="append", help="只看这些服务（可重复；缺省 = 全部启用的）"
    )

    grant = sub.add_parser("grant", help="给人 / 部门授某个服务")
    grant.add_argument("service")
    _add_actor(grant, "操作人（要是该服务的所有者）")
    who = grant.add_mutually_exclusive_group(required=True)
    who.add_argument("--all-users", action="store_true", help="根部门（含下级）= 全员")
    who.add_argument("--user")
    who.add_argument("--dept", help="部门（含下级）")
    grant.add_argument("--owner", action="store_true", help="授所有者（缺省 = 使用）")

    imp = sub.add_parser("import", help="把 Atlas 原来的 MCP 服务登记进网关")
    owner = imp.add_mutually_exclusive_group(required=True)
    owner.add_argument("--owner", help="注册人（用户中心 uid）")
    owner.add_argument("--owner-account", help="注册人的用户中心账号")
    imp.add_argument("--from-env", required=True, help="原来 atlas-server 的 MCP_SERVERS（JSON）")

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
        if args.command == "service":
            return service_command(args)
        if args.command == "review":
            return review_command(args)
        if args.command == "key":
            return key_command(args)
        if args.command == "register-uc":
            return register_command(args)
        if args.command == "import":
            return import_command(args)
        if args.command == "grant":
            return grant_command(args)
    except GatewayError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
