"""模型网关的入口与运维命令。

    python -m atlas_modelgateway serve                     起 HTTP 服务（默认 :8090）
    python -m atlas_modelgateway migrate                   升级网关库到最新
    python -m atlas_modelgateway register-uc --account admin
                                     把应用、操作码、角色、数据编码 Model 登记到用户中心
    python -m atlas_modelgateway model list
    python -m atlas_modelgateway model add <平台模型名> --owner <uid> [--kind chat]
                                     [--upstream <上游模型名>] [--protocol anthropic …]
                                     [--context-window N] [--max-output N] [--provider …]
    python -m atlas_modelgateway model activate <平台模型名> --actor <uid>
    python -m atlas_modelgateway model probe <平台模型名> --actor <uid> [--apply]
    python -m atlas_modelgateway price add <平台模型名> --input 2 --output 8 --actor <uid>
                                     [--currency CNY]（每百万 token）
    python -m atlas_modelgateway purpose set <用途> --default <平台模型名> [--allow …] --actor <uid>
    python -m atlas_modelgateway grant <平台模型名> --actor <uid>
                                     (--all-users | --user | --dept) [--owner]

  迁移（§6.3「存量迁移」，可以重复执行）：
    python -m atlas_agent export-models > atlas-models.json      （在 Atlas 那边导出）
    python -m atlas_modelgateway import --file atlas-models.json --owner-account admin [--all-users]
                                     登记模型、上线原来可用的；--all-users 给根部门授「使用」；
                                     数字员工按 Agent 现有配置授权；平台用途沿用原来的模型

★ 命令行和管理接口调同一个 Registry：规则只有一份实现。管理端页面做出来之前，部署时用命令行。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from .auth import use_outbound
from .db.models import GwModelProbe
from .db.session import get_engine, get_sessionmaker
from .errors import GatewayError
from .perms import Grant, UCPermissions
from .registry import Registry
from .settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_MODELGATEWAY_PORT：Service 名叫 atlas-modelgateway 时 kubelet 会注入
    ATLAS_MODELGATEWAY_PORT=tcp://… 这种遗留变量（atlas-config 踩过同一个坑）。"""
    raw = os.environ.get("ATLAS_MODELGW_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8090


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=os.environ.get("ATLAS_MODELGW_HOST", "0.0.0.0"), port=_port())


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


def _print_model(row: Any) -> None:
    state = (
        "停用"
        if row.disabled
        else {"onboarding": "接入中", "available": "可用", "deprecated": "弃用", "retired": "下线"}[
            row.status
        ]
    )
    check = {True: "接入检查通过", False: "⚠ 可以直连", None: "接入检查未完成"}[row.access_check_ok]
    protocols = ",".join(row.protocols or []) or "无协议"
    print(
        f"{row.name:<28} {row.kind:<9} {state:<4} 上游 {row.upstream_model:<28} "
        f"{protocols:<16} {check}"
    )


def _with_perms(
    action: Callable[[Any, UCPermissions, str], Awaitable[Any]], args: argparse.Namespace
) -> Any:
    """要用户中心的命令：解析操作人、按他建立出站身份。"""
    settings = get_settings()

    async def run(session: Any) -> Any:
        perms = UCPermissions(getattr(args, "uc_url", None) or settings.uc_base_url)
        try:
            actor = await _uid(
                perms,
                getattr(args, "actor", None) or getattr(args, "owner", None),
                getattr(args, "actor_account", None) or getattr(args, "owner_account", None),
            )
            use_outbound({"X-Uid": actor, "X-Auth-Uid": actor})
            return await action(session, perms, actor)
        finally:
            await perms.aclose()

    return asyncio.run(_with_session(run))


def model_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.action == "list":

        async def _list(session: Any) -> None:
            for row in await Registry(session, settings).list():
                _print_model(row)

        asyncio.run(_with_session(_list))
        return 0
    if args.action == "add":
        attrs: dict[str, Any] = {
            "upstream_model": args.upstream,
            "display_name": args.display_name,
            "provider": args.provider,
            "protocols": args.protocol,
            "context_window": args.context_window,
            "max_output_tokens": args.max_output,
            "capabilities": json.loads(args.capabilities) if args.capabilities else None,
        }
        attrs = {k: v for k, v in attrs.items() if v is not None}

        async def _add(session: Any, perms: UCPermissions, actor: str) -> Any:
            return await Registry(session, settings).create(
                perms, name=args.name, kind=args.kind, actor=actor, **attrs
            )

        _print_model(_with_perms(_add, args))
        return 0
    if args.action == "activate":

        async def _activate(session: Any) -> Any:
            return await Registry(session, settings).activate(args.name, actor=args.actor)

        _print_model(asyncio.run(_with_session(_activate)))
        return 0
    if args.action == "probe":
        from .probe import run_probe

        async def _probe(session: Any) -> Any:
            registry = Registry(session, settings)
            row = await registry.get(args.name)
            await registry.check_access(row)
            outcome = await run_probe(settings, row)
            probe = GwModelProbe(
                model_uuid=row.uuid,
                results=[r.__dict__ for r in outcome.results],
                suggested=outcome.suggested,
                differences=outcome.differences,
                created_by=args.actor,
            )
            session.add(probe)
            await session.flush()
            for r in outcome.results:
                print(f"  {'✓' if r.ok else '✗'} {r.protocol:<9} {r.check:<20} {r.detail[:100]}")
            print("和目录不一致：" + (json.dumps(outcome.differences, ensure_ascii=False) or "无"))
            if args.apply:
                await registry.apply_probe(args.name, probe.uuid, actor=args.actor)
                print("✓ 已按探测结果更新目录")
            return row

        _print_model(asyncio.run(_with_session(_probe)))
        return 0
    raise AssertionError(args.action)


def price_command(args: argparse.Namespace) -> int:
    async def _add(session: Any) -> Any:
        return await Registry(session, get_settings()).add_price(
            args.name,
            effective_from=None,
            actor=args.actor,
            currency=args.currency,
            input=Decimal(args.input),
            output=Decimal(args.output),
            cache_read=Decimal(args.cache_read) if args.cache_read else None,
            cache_write=Decimal(args.cache_write) if args.cache_write else None,
        )

    row = asyncio.run(_with_session(_add))
    print(
        f"✓ 已新增价目 {args.name}：输入 {row.input} / 输出 {row.output} {row.currency}"
        "（每百万 token）"
    )
    return 0


def purpose_command(args: argparse.Namespace) -> int:
    async def _set(session: Any) -> Any:
        return await Registry(session, get_settings()).set_purpose(
            args.code, default_model=args.default, allowed_models=args.allow or [], actor=args.actor
        )

    row = asyncio.run(_with_session(_set))
    print(f"✓ 平台用途 {row.code}：默认 {row.default_model}，允许 {', '.join(row.allowed_models)}")
    return 0


def grant_command(args: argparse.Namespace) -> int:
    from .migrate import grant_all_users, root_dept

    settings = get_settings()

    async def _grant(session: Any, perms: UCPermissions, actor: str) -> None:
        row = await Registry(session, settings).get(args.model)
        if row.kind != "chat":
            raise GatewayError(f"{row.kind} 模型只供平台功能使用，不按人授权")
        level = "OWNER" if args.owner else "READ"
        if args.all_users:
            await grant_all_users(
                perms, args.model, root=await root_dept(settings.uc_base_url), operator=actor
            )
        elif args.user:
            await perms.grant(args.model, actor, [Grant(level, "user", args.user)])
        else:
            await perms.grant(
                args.model, actor, [Grant(level, "dept", args.dept, include_sub=True)]
            )

    _with_perms(_grant, args)
    print(f"✓ 已授权 {args.model}")
    return 0


def import_command(args: argparse.Namespace) -> int:
    from .migrate import import_catalog, root_dept

    settings = get_settings()
    exported = json.loads(Path(args.file).read_text())

    async def _import(session: Any, perms: UCPermissions, owner: str) -> Any:
        root = await root_dept(settings.uc_base_url) if args.all_users else None
        return await import_catalog(session, settings, perms, exported, owner=owner, root=root)

    report = _with_perms(_import, args)
    for name in report.imported:
        print(f"✓ 已登记 {name}{'（已上线）' if name in report.activated else ''}")
    for name in report.existing:
        print(f"· {name} 已经登记过")
    for line in report.granted:
        print(f"✓ 授权 {line}")
    for line in report.purposes:
        print(f"✓ 平台用途 {line}")
    for name, why in report.skipped:
        print(f"✗ {name}：{why}")
    return 1 if report.skipped else 0


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
    parser = argparse.ArgumentParser(prog="python -m atlas_modelgateway")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")

    model = sub.add_parser("model").add_subparsers(dest="action", required=True)
    model.add_parser("list")
    add = model.add_parser("add")
    add.add_argument("name")
    owner = add.add_mutually_exclusive_group(required=True)
    owner.add_argument("--owner", help="登记人（用户中心 uid），对话模型的所有者")
    owner.add_argument("--owner-account", help="登记人的用户中心账号")
    add.add_argument("--kind", choices=["chat", "embedding", "rerank"], default="chat")
    add.add_argument("--upstream", help="上游模型名（缺省 = 平台模型名）")
    add.add_argument("--protocol", action="append", choices=["anthropic", "openai", "rerank"])
    add.add_argument("--display-name")
    add.add_argument("--provider")
    add.add_argument("--context-window", type=int)
    add.add_argument("--max-output", type=int)
    add.add_argument("--capabilities", help="能力（JSON，catalog.Capabilities）")
    add.add_argument("--uc-url", default=None)
    act = model.add_parser("activate")
    act.add_argument("name")
    act.add_argument("--actor", required=True)
    probe = model.add_parser("probe", help="跑一次能力探测（会花真实的 token）")
    probe.add_argument("name")
    probe.add_argument("--actor", required=True)
    probe.add_argument("--apply", action="store_true", help="按探测结果直接更新目录")

    price = sub.add_parser("price").add_subparsers(dest="action", required=True)
    padd = price.add_parser("add", help="新增一版价目（每百万 token）")
    padd.add_argument("name")
    padd.add_argument("--input", required=True)
    padd.add_argument("--output", required=True)
    padd.add_argument("--cache-read")
    padd.add_argument("--cache-write")
    padd.add_argument("--currency", default="CNY")
    padd.add_argument("--actor", required=True)

    purpose = sub.add_parser("purpose").add_subparsers(dest="action", required=True)
    pset = purpose.add_parser("set")
    pset.add_argument("code")
    pset.add_argument("--default")
    pset.add_argument("--allow", action="append")
    pset.add_argument("--actor", required=True)

    grant = sub.add_parser("grant", help="给人 / 部门授某个对话模型")
    grant.add_argument("model")
    _add_actor(grant, "操作人（要是该模型的所有者）")
    who = grant.add_mutually_exclusive_group(required=True)
    who.add_argument("--all-users", action="store_true", help="根部门（含下级）= 全员")
    who.add_argument("--user")
    who.add_argument("--dept", help="部门（含下级）")
    grant.add_argument("--owner", action="store_true", help="授所有者（缺省 = 使用）")

    imp = sub.add_parser("import", help="把 Atlas 导出的模型目录登记进网关")
    imp.add_argument("--file", required=True, help="python -m atlas_agent export-models 的输出")
    who = imp.add_mutually_exclusive_group(required=True)
    who.add_argument("--owner", help="登记人（用户中心 uid）")
    who.add_argument("--owner-account", help="登记人的用户中心账号")
    imp.add_argument("--all-users", action="store_true", help="给新登记的对话模型授全员「使用」")

    reg = sub.add_parser("register-uc", help="把应用、操作码、数据编码登记到用户中心")
    reg.add_argument("--account", required=True, help="用户中心管理员账号")
    reg.add_argument("--uc-url", default=None)
    reg.add_argument("--tenant", default=None, help="管理员所在的企业代码（有多个租户时必填）")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    commands: dict[str, Callable[[argparse.Namespace], int]] = {
        "model": model_command,
        "price": price_command,
        "purpose": purpose_command,
        "grant": grant_command,
        "import": import_command,
        "register-uc": register_command,
    }
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "migrate":
            migrate()
            return 0
        return commands[args.command](args)
    except GatewayError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
