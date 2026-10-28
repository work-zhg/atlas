"""模型管理平台的入口与运维命令。

    python -m atlas_modelgateway serve                     起 HTTP 服务（默认 :8090）
    python -m atlas_modelgateway migrate                   升级平台库到最新
    python -m atlas_modelgateway register-uc --account admin
                                     把应用、操作码、角色、数据编码 Model 登记到用户中心
    python -m atlas_modelgateway model list           平台登记的 + LiteLLM 直配的模型
    python -m atlas_modelgateway model add deepseek-chat --provider deepseek \
                --api-key-env DEEPSEEK_API_KEY [--display-name …] [--visibility public] \
                [--api-base …] [--max-input N] [--max-output N] \
                [--input-price 美元/百万] [--output-price …] --actor-account admin
                                     登记模型（管理端页面做出来之前，部署脚本用它）
    python -m atlas_modelgateway client-key ensure --type Service --code session-title \\
                [--name 会话标题] [--models deepseek-flash,…] --actor-account admin
                                     建一把客户端 Key（已有就沿用）；给了 --models 就分配模型；
                                     --if-pending：已经分配过的不覆盖（批量给存量智能体分配时用）
    python -m atlas_modelgateway reconcile                  对一次账（平时由服务里的对账任务定期做）

★ 命令行和管理接口调同一套 ModelRegistry / KeyService：规则只有一份实现。
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

from .auth import use_outbound
from .db.session import get_engine, get_sessionmaker
from .errors import GatewayError, Invalid
from .keys import KeyService
from .litellm_api import LiteLLM
from .perms import SubjectsCache, UCPermissions
from .reconcile import reconcile_once
from .registry import DeploymentsCache, ModelFields, ModelRegistry, RouteFields
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


class _Tools:
    """命令行用的一套依赖：库会话、LiteLLM、用户中心。"""

    def __init__(self) -> None:
        s = get_settings()
        self.litellm = LiteLLM(
            s.litellm_base_url,
            s.litellm_master_key.get_secret_value() if s.litellm_master_key else None,
            timeout_s=s.litellm_timeout_s,
        )
        self.deployments = DeploymentsCache(self.litellm, 0)
        self.perms = UCPermissions(s.uc_base_url)
        self.subjects = SubjectsCache(self.perms, 0, 0)

    async def run(self, fn: Callable[[Any, KeyService], Awaitable[Any]]) -> Any:
        try:
            async with get_sessionmaker()() as session:
                registry = ModelRegistry(session, self.litellm, self.deployments, self.perms)
                result = await fn(
                    registry, KeyService(session, self.litellm, registry, self.subjects)
                )
                await session.commit()
                return result
        finally:
            await self.litellm.aclose()
            await self.perms.aclose()
            await get_engine().dispose()

    async def actor(self, uid: str | None, account: str | None) -> str:
        """命令行里的操作人：直接给 uid，或给用户中心账号（部署脚本只知道账号）。"""
        if uid:
            return uid
        assert account is not None
        user = await self.perms.user_by_account(account)
        if user is None:
            raise GatewayError(f"用户中心里没有账号 {account}")
        use_outbound({"X-Uid": str(user["id"])})
        return str(user["id"])


def model_command(args: argparse.Namespace) -> int:
    async def run(registry: ModelRegistry, _: KeyService) -> None:
        views, reachable = await registry.listing(fresh=True)
        if not reachable:
            print("⚠ LiteLLM 不可达：状态未知")
        for v in views:
            if v.row is None:
                state, label = "LiteLLM 直配", ""
            else:
                state = "LiteLLM 中缺失" if v.missing else "可调用"
                label = "公有" if v.row.visibility == "public" else "私有"
            print(f"{v.name:<40} {state:<12} {label}")

    asyncio.run(_Tools().run(run))
    return 0


def model_add_command(args: argparse.Namespace) -> int:
    tools = _Tools()
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    if args.api_key_env and not api_key:
        raise Invalid(f"环境变量 {args.api_key_env} 是空的")

    async def run(registry: ModelRegistry, _: KeyService) -> None:
        actor = await tools.actor(args.actor, args.actor_account)
        if await registry.by_name(args.name) is not None:
            print(f"· 已登记 {args.name}")
            return
        await registry.register(
            name=args.name,
            fields=ModelFields(args.display_name or args.name, args.provider, args.visibility),
            route=RouteFields(
                api_base=args.api_base or None,
                api_key=api_key or None,
                max_input_tokens=args.max_input,
                max_output_tokens=args.max_output,
                input_price=args.input_price,
                output_price=args.output_price,
            ),
            grants=[],
            actor=actor,
        )
        print(f"✓ 已登记 {args.name}（{args.visibility}）")

    asyncio.run(tools.run(run))
    return 0


def client_key_command(args: argparse.Namespace) -> int:
    tools = _Tools()

    async def run(_: ModelRegistry, keys: KeyService) -> None:
        settings = get_settings()
        if args.type not in settings.client_types:
            raise Invalid(f"没有登记的客户端类型：{args.type}（见 ATLAS_MODELGW_CLIENT_TYPES）")
        actor = await tools.actor(args.actor, args.actor_account)
        key, created = await keys.ensure_client(
            client_type=args.type, client_code=args.code, name=args.name or "", actor=actor
        )
        print(f"{'✓ 已创建' if created else '· 已存在'} 客户端 Key {args.type}:{args.code}")
        if args.models and args.if_pending and key.status != "pending":
            print(f"· 已经分配过模型（{', '.join(key.models)}），不覆盖")
        elif args.models:
            models = [m.strip() for m in args.models.split(",") if m.strip()]
            await keys.assign(key, models, actor=actor)
            print(f"✓ 已分配模型：{', '.join(key.models)}（状态 {key.status}）")

    asyncio.run(tools.run(run))
    return 0


def reconcile_command(_: argparse.Namespace) -> int:
    async def run() -> None:
        tools = _Tools()
        try:
            report = await reconcile_once(
                get_sessionmaker(), tools.litellm, tools.deployments, tools.perms, tools.subjects
            )
        finally:
            await tools.litellm.aclose()
            await tools.perms.aclose()
            await get_engine().dispose()
        print(f"检查 {report.checked} 把 Key，修正 {report.changed} 把，{report.failed} 把同步失败")

    asyncio.run(run())
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
    parser = argparse.ArgumentParser(prog="python -m atlas_modelgateway")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    sub.add_parser("reconcile", help="对一次账：把 Key 的有效模型、停用状态同步到 LiteLLM")

    model = sub.add_parser("model").add_subparsers(dest="action", required=True)
    model.add_parser("list")
    add = model.add_parser("add", help="登记模型（已登记就跳过）")
    add.add_argument("name", help="模型名（供应商侧的名字，也是调用时传的 model）")
    add.add_argument("--provider", required=True)
    add.add_argument("--display-name", default="")
    add.add_argument("--visibility", choices=["public", "private"], default="private")
    add.add_argument("--api-base", default="")
    add.add_argument(
        "--api-key-env", default="", help="供应商凭据所在的环境变量（不在命令行里写原文）"
    )
    add.add_argument("--max-input", type=int, default=None)
    add.add_argument("--max-output", type=int, default=None)
    add.add_argument("--input-price", type=float, default=None, help="美元 / 百万 token")
    add.add_argument("--output-price", type=float, default=None, help="美元 / 百万 token")
    add_actor = add.add_mutually_exclusive_group(required=True)
    add_actor.add_argument("--actor", help="操作人（用户中心 uid）")
    add_actor.add_argument("--actor-account", help="操作人的用户中心账号")

    ck = sub.add_parser("client-key").add_subparsers(dest="action", required=True)
    ensure = ck.add_parser("ensure", help="建一把客户端 Key（已有就沿用），可以同时分配模型")
    ensure.add_argument(
        "--type", required=True, help="客户端类型（Agent、Service …）"
    )
    ensure.add_argument("--code", required=True, help="客户端编码")
    ensure.add_argument("--name", default="", help="展示用的名字")
    ensure.add_argument("--models", default="", help="分配的模型，逗号分隔")
    ensure.add_argument("--if-pending", action="store_true", help="已经分配过模型的不覆盖")
    actor = ensure.add_mutually_exclusive_group(required=True)
    actor.add_argument("--actor", help="操作人（用户中心 uid）")
    actor.add_argument("--actor-account", help="操作人的用户中心账号")

    reg = sub.add_parser("register-uc", help="把应用、操作码、数据编码登记到用户中心")
    reg.add_argument("--account", required=True, help="用户中心管理员账号")
    reg.add_argument("--uc-url", default=None)
    reg.add_argument("--tenant", default=None, help="管理员所在的企业代码（有多个租户时必填）")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    commands: dict[str, Callable[[argparse.Namespace], int]] = {
        "model": lambda a: model_add_command(a) if a.action == "add" else model_command(a),
        "client-key": client_key_command,
        "reconcile": reconcile_command,
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
