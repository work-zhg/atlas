"""MCP 网关的入口与运维命令。

    python -m atlas_mcpgateway serve                     起 HTTP 服务（默认 :8070）
    python -m atlas_mcpgateway migrate                   升级网关库到最新
    python -m atlas_mcpgateway register-uc --account admin
                                     登记应用、操作码、角色、数据编码（McpServer、McpTool）
    python -m atlas_mcpgateway service list
    python -m atlas_mcpgateway service refresh <服务名>   MCP 代理：立即从上游拉一次工具定义
    python -m atlas_mcpgateway service add --file <spec.json|-> --owner-account admin
                                     按文件登记一个服务（部署脚本用；已存在就跳过）

★ 登记服务、管理 Key、授权都在管理端页面上做（管理接口）；命令行只留部署要用的。
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
from .errors import GatewayError
from .perms import UCPermissions
from .registry import Registry
from .settings import get_settings
from .upstreams.hub import Upstreams

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


async def _with_registry(fn: Callable[[Registry], Awaitable[Any]]) -> Any:
    settings = get_settings()
    perms = UCPermissions(settings.uc_base_url)
    upstreams = Upstreams(settings)
    try:
        async with get_sessionmaker()() as session:
            result = await fn(Registry(session, settings, perms, upstreams))
            await session.commit()
            return result
    finally:
        await upstreams.aclose()
        await perms.aclose()
        await get_engine().dispose()


def _print_service(row: Any) -> None:
    state = "启用" if row.status == "enabled" else "停用"
    fetched = f"  拉取失败：{row.fetch_error}" if row.fetch_error else ""
    print(f"{row.name:<16} {row.kind:<5} {row.visibility:<8} {state}{fetched}")


def _load_spec(path: str) -> dict[str, Any]:
    """登记文件：管理接口 POST /services 的请求体，外加 REST 手工定义的 tools。

    ★ 凭据的值（credential.secret）不入库：部署脚本从 .env 填进去，经标准输入（path = -）传进 Pod。
    """
    import yaml

    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    spec = yaml.safe_load(text)  # JSON 是 YAML 的子集
    if not isinstance(spec, dict) or not spec.get("name") or not spec.get("kind"):
        raise GatewayError("登记文件要有 name、kind")
    return spec


async def add_service(registry: Registry, spec: dict[str, Any], *, owner: str) -> Any:
    """按登记文件登记一个服务（owner 成为所有者）。已存在 → None（不改它）。"""
    if await registry.find(spec["name"]) is not None:
        print(f"· MCP 服务 {spec['name']} 已存在，跳过（要改请在管理端上改）")
        return None
    credential = dict(spec.get("credential") or {"type": "none"})
    secret = (credential.pop("secret", None) or "").strip() or None
    row = await registry.create(
        kind=spec["kind"],
        name=spec["name"],
        display_name=spec.get("display_name") or spec["name"],
        description=spec.get("description") or "",
        visibility=spec.get("visibility") or "private",
        connection=spec.get("connection") or {},
        credential=credential,
        secret=secret,
        pass_identity=bool(spec.get("pass_identity", True)),
        selected=spec.get("selected") or [],
        descriptor=None,
        actor=owner,
    )
    for tool in spec.get("tools") or []:
        await registry.add_tool(
            row.name,
            tool_name=tool["name"],
            description=tool["description"],
            effect=tool.get("effect") or "read",
            mapping=tool["mapping"],
            actor=owner,
        )
    return row


def service_command(args: argparse.Namespace) -> int:
    if args.action == "add":
        spec = _load_spec(args.file)

        async def _add(registry: Registry) -> Any:
            perms = registry.perms
            assert isinstance(perms, UCPermissions)  # 命令行只连真实的用户中心
            user = await perms.user_by_account(args.owner_account)
            if user is None:
                raise GatewayError(f"用户中心里没有账号 {args.owner_account}")
            return await add_service(registry, spec, owner=str(user["id"]))

        row = asyncio.run(_with_registry(_add))
        if row is not None:
            _print_service(row)
        return 0
    if args.action == "list":

        async def _list(registry: Registry) -> None:
            for row in await registry.list():
                _print_service(row)
                for tool in await registry.tools(row):
                    print(f"    {tool.name:<32} {tool.effect:<12} {tool.status}")

        asyncio.run(_with_registry(_list))
        return 0

    async def _refresh(registry: Registry) -> Any:
        return await registry.refresh(await registry.get(args.name))

    _print_service(asyncio.run(_with_registry(_refresh)))
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
    ref = svc.add_parser("refresh", help="MCP 代理：立即从上游拉一次工具定义")
    ref.add_argument("name")
    add = svc.add_parser("add", help="按文件登记一个服务（已存在就跳过）")
    add.add_argument("--file", required=True, help="登记文件（JSON / YAML）；- = 标准输入")
    add.add_argument("--owner-account", required=True, help="登记人（成为所有者）的用户中心账号")

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
        if args.command == "register-uc":
            return register_command(args)
    except GatewayError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
