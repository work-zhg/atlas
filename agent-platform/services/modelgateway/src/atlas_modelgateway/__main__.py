"""模型平台的入口与运维命令。

    python -m atlas_modelgateway serve                     管理平台（默认 :8090）
    python -m atlas_modelgateway gateway                   模型网关（默认 :8091）
    python -m atlas_modelgateway migrate                   升级平台库到最新（含运营接口的两张表）

  下面的命令都在一个租户里做（--tenant 租户 uuid），操作人用 --actor（uid）或 --actor-account：
    account list | account add --name 公司账号 --provider deepseek --secret-env DEEPSEEK_API_KEY \
                [--endpoint anthropic=https://… ...] [--egress proxy]
    model list | model add deepseek-v4-pro --route 公司账号[:上游模型[:优先级[:权重]]] ... \
                [--display-name …] [--visibility public] [--kind chat] [--price 4,0.4,16]
    client-key ensure --type Service --code session-title [--name 会话标题] \
                [--models a,b] [--if-pending]
    smoke --client Service:session-title --model deepseek-flash [--url http://127.0.0.1:8091]
                                     用这把客户端 Key 经网关的通用接口（Atlas 用的那个）调一次模型
                                     （最多 16 个 token），看通不通

  全部租户：
    encrypt-secrets                  客户端 Key 的明文原文改成加密存；
                                     用户 Key 的原文删掉（只留摘要）
    refresh-keys                     重算全部用户 Key 的有效模型（平时由定时任务做）

★ 命令行和管理接口调同一套 AccountService / ModelRegistry / KeyService：规则只有一份实现。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope, TenantSweep
from sqlalchemy import select

from .accounts import AccountInput, AccountService
from .auth import use_outbound
from .crypto import SecretCipher
from .db.models import GwApiKey
from .db.session import get_engine, get_sessionmaker
from .errors import GatewayError, Invalid, NotFound
from .keys import KeyService
from .perms import SubjectsCache, UCPermissions
from .provider import ProviderCatalog
from .registry import ModelInput, ModelRegistry, RouteInput
from .settings import get_settings
from .shared.health import RouteHealth
from .shared.kernel import ModelKind, Price
from .shared.upstream import AddressGuard

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


def _gateway_port() -> int:
    raw = os.environ.get("ATLAS_MODELGW_GATEWAY_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8091


def gateway() -> None:
    """模型网关：所有模型调用的入口（和管理平台分开部署）。"""
    import uvicorn

    from .gateway.app import create_gateway_app

    uvicorn.run(
        create_gateway_app(),
        host=os.environ.get("ATLAS_MODELGW_HOST", "0.0.0.0"),
        port=_gateway_port(),
        timeout_graceful_shutdown=get_settings().drain_timeout_s,
    )


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)
    asyncio.run(_upgrade_opsapi())


async def _upgrade_opsapi() -> None:
    """运营接口库的两张表（租户状态、运营请求；各自的版本表）。"""
    from atlas_opsapi import upgrade
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(upgrade)
    finally:
        await engine.dispose()


class _Tools:
    """命令行用的一套依赖：库会话、用户中心、主密钥。"""

    def __init__(self) -> None:
        s = get_settings()
        self.perms = UCPermissions(s.uc_base_url)
        self.subjects = SubjectsCache(self.perms, 0, 0)
        self.cipher = SecretCipher(s.master_keys())
        self.catalog = ProviderCatalog()
        self.guard = AddressGuard(s.upstream_allow_cidrs)

    async def run(self, tenant: UUID | None, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            if tenant is None:
                return await fn(None)
            with TenantScope.enter(tenant):
                async with get_sessionmaker()() as session:
                    result = await fn(session)
                    await session.commit()
                    return result
        finally:
            await self.perms.aclose()
            await get_engine().dispose()

    def registry(self, session: Any) -> ModelRegistry:
        return ModelRegistry(session, self.perms)

    def keys(self, session: Any) -> KeyService:
        return KeyService(session, self.registry(session), self.subjects, self.cipher)

    def accounts(self, session: Any) -> AccountService:
        return AccountService(
            session,
            cipher=self.cipher,
            catalog=self.catalog,
            guard=self.guard,
            health=RouteHealth(None),
        )

    async def actor(self, uid: str | None, account: str | None) -> str:
        """命令行里的操作人：直接给 uid，或给用户中心账号（部署脚本只知道账号）。"""
        if uid:
            use_outbound({"X-Uid": uid})
            return uid
        assert account is not None
        user = await self.perms.user_by_account(account)
        if user is None:
            raise GatewayError(f"用户中心里没有账号 {account}")
        use_outbound({"X-Uid": str(user["id"])})
        return str(user["id"])


def _secret_from_env(name: str) -> str:
    if not name:
        return ""
    value = os.environ.get(name, "").strip()
    if not value:
        raise Invalid(f"环境变量 {name} 是空的")
    return value


def account_command(args: argparse.Namespace) -> int:
    tools = _Tools()

    async def run(session: Any) -> None:
        accounts = tools.accounts(session)
        if args.action == "list":
            for a in await accounts.list():
                print(
                    f"{a.name}\t{a.provider}\t{a.egress}\t"
                    f"{'启用' if a.enabled else '停用'}\t{dict(a.endpoints)}"
                )
            return
        actor = await tools.actor(args.actor, args.actor_account)
        spec = tools.catalog.spec(args.provider)
        endpoints = (
            dict(e.split("=", 1) for e in args.endpoint)
            if args.endpoint
            else {f.value: spec.endpoints.get(f, "") for f in spec.formats[:1]}
        )
        found = next((a for a in await accounts.list() if a.name == args.name), None)
        if found is not None:
            print(f"账号「{args.name}」已经有了，跳过")
            return
        egress = args.egress or ("proxy" if spec.overseas else "direct")
        row = await accounts.create(
            AccountInput(
                args.name, args.provider, endpoints, _secret_from_env(args.secret_env), egress
            ),
            actor=actor,
        )
        print(f"已添加账号「{row.name}」（{row.uuid}）")

    asyncio.run(tools.run(UUID(args.tenant), run))
    return 0


def _routes(registry_accounts: dict[str, Any], specs: list[str]) -> list[RouteInput]:
    out = []
    for spec in specs:
        name, *rest = spec.split(":")
        account = registry_accounts.get(name)
        if account is None:
            raise NotFound(f"没有叫「{name}」的账号")
        upstream = rest[0] if rest else ""
        priority = int(rest[1]) if len(rest) > 1 else 1
        weight = int(rest[2]) if len(rest) > 2 else 1
        out.append(RouteInput(account.uuid, upstream, priority, weight))
    return out


def model_command(args: argparse.Namespace) -> int:
    tools = _Tools()

    async def run(session: Any) -> None:
        registry = tools.registry(session)
        if args.action == "list":
            routes = await registry.routes()
            accounts = await registry.accounts()
            for m in await registry.rows():
                lines = ", ".join(
                    f"{accounts[r.account_uuid].name}:{r.upstream_model}"
                    for r in routes.get(m.uuid, [])
                    if r.account_uuid in accounts
                )
                print(
                    f"{m.name}\t{m.kind}\t{m.visibility}\t"
                    f"{'启用' if m.enabled else '停用'}\t{lines or '（没有线路）'}"
                )
            return
        actor = await tools.actor(args.actor, args.actor_account)
        if await registry.by_name(args.name) is not None:
            print(f"模型 {args.name} 已经登记过了，跳过")
            return
        by_name = {a.name: a for a in (await registry.accounts()).values()}
        price = Price(*(Decimal(x) for x in args.price.split(","))) if args.price else Price()
        row = await registry.create(
            args.name,
            ModelInput(
                display_name=args.display_name or args.name,
                kind=ModelKind(args.kind),
                visibility=args.visibility,
                price=price,
                routes=_routes(by_name, args.route),
            ),
            actor=actor,
        )
        print(f"已登记模型 {row.name}")

    asyncio.run(tools.run(UUID(args.tenant), run))
    return 0


def client_key_command(args: argparse.Namespace) -> int:
    tools = _Tools()

    async def run(session: Any) -> None:
        keys = tools.keys(session)
        actor = await tools.actor(args.actor, args.actor_account)
        key, created = await keys.ensure_client(
            client_type=args.type, client_code=args.code, name=args.name, actor=actor
        )
        models = [m for m in args.models.split(",") if m.strip()]
        if models and not (args.if_pending and key.status != "pending"):
            await keys.assign(key, models, actor=actor)
        print(
            f"{'新建' if created else '沿用'}客户端 Key {args.type}:{args.code}"
            f"（{key.status}，模型 {key.models}）"
        )

    asyncio.run(tools.run(UUID(args.tenant), run))
    return 0


def encrypt_secrets_command(_: argparse.Namespace) -> int:
    tools = _Tools()
    if not tools.cipher.configured:
        raise Invalid("没有配置主密钥（ATLAS_MODELGW_SECRET_KEYS）")

    async def run(_: Any) -> None:
        async def fetch() -> list[tuple[UUID, UUID]]:
            async with get_sessionmaker()() as session:
                rows = await session.execute(
                    select(GwApiKey.uuid, GwApiKey.tenant_uuid).where(GwApiKey.secret.is_not(None))
                )
                return list(rows)

        async def handle(key_uuid: UUID) -> None:
            async with get_sessionmaker()() as session:
                key = await session.scalar(select(GwApiKey).where(GwApiKey.uuid == key_uuid))
                if key is None or not key.secret or key.secret.startswith("v1:"):
                    return
                key.secret = tools.cipher.encrypt(key.secret) if key.key_type == "client" else None
                await session.commit()

        ok, failed = await TenantSweep.each(fetch, handle)
        print(f"处理了 {ok} 把 Key，失败 {failed} 把")

    asyncio.run(tools.run(None, run))
    return 0


def refresh_keys_command(_: argparse.Namespace) -> int:
    tools = _Tools()

    async def run(_: Any) -> None:
        from .jobs import KeyRefresh

        await KeyRefresh(get_sessionmaker(), tools.perms, tools.subjects, tools.cipher).execute(
            None, None
        )  # type: ignore[arg-type]

    asyncio.run(tools.run(None, run))
    return 0


def smoke_command(args: argparse.Namespace) -> int:
    """部署后确认通路：Key 认得、模型有线路、上游回得了话。不打印 Key 原文。"""
    import httpx

    client_type, _, code = args.client.partition(":")
    if not code:
        raise Invalid("--client 要写成「类型:编码」，如 Service:session-title")
    tools = _Tools()

    async def run(session: Any) -> str:
        keys = tools.keys(session)
        key = await keys.client_key(client_type, code)
        secret = keys.reveal(key) if key else None
        if not secret:
            raise Invalid(f"没有客户端 Key {args.client}（或原文取不出来）")
        return secret

    secret = asyncio.run(tools.run(UUID(args.tenant), run))
    resp = httpx.post(
        f"{args.url.rstrip('/')}/universal/v1/messages",
        headers={"x-api-key": secret, "anthropic-version": "2023-06-01"},
        json={
            "model": args.model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "回一个字：好"}],
        },
        timeout=args.timeout,
    )
    is_json = resp.headers.get("content-type", "").startswith("application/json")
    body = resp.json() if is_json else {}
    if resp.status_code != 200:
        print(f"✗ {resp.status_code} {body.get('error') or resp.text[:300]}")
        return 1
    text = "".join(b.get("text", "") for b in body.get("content") or [] if b.get("type") == "text")
    print(f"✓ {args.model} 回了：{text.strip()[:40]!r}，用量 {body.get('usage')}")
    return 0


def _actor_args(p: argparse.ArgumentParser) -> None:
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--actor", help="操作人（用户中心 uid）")
    group.add_argument("--actor-account", help="操作人的用户中心账号")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_modelgateway")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("gateway", help="模型网关：所有模型调用的入口")
    sub.add_parser("migrate")
    sub.add_parser("encrypt-secrets", help="客户端 Key 的原文加密存，用户 Key 的原文删掉")
    sub.add_parser("refresh-keys", help="重算全部用户 Key 的有效模型")

    account = sub.add_parser("account").add_subparsers(dest="action", required=True)
    for action in ("list", "add"):
        p = account.add_parser(action)
        p.add_argument("--tenant", required=True)
    add = account.choices["add"]
    add.add_argument("--name", required=True)
    add.add_argument("--provider", required=True)
    add.add_argument(
        "--endpoint",
        action="append",
        default=[],
        help="格式=地址，可以给多个；不给用供应商默认地址",
    )
    add.add_argument("--secret-env", default="", help="密钥所在的环境变量（不在命令行里写原文）")
    add.add_argument("--egress", choices=["direct", "proxy"], default=None)
    _actor_args(add)

    model = sub.add_parser("model").add_subparsers(dest="action", required=True)
    for action in ("list", "add"):
        p = model.add_parser(action)
        p.add_argument("--tenant", required=True)
    madd = model.choices["add"]
    madd.add_argument("name", help="模型名（调用时传的 model）")
    madd.add_argument(
        "--route", action="append", required=True, help="账号名[:上游模型[:优先级[:权重]]]"
    )
    madd.add_argument("--display-name", default="")
    madd.add_argument("--visibility", choices=["public", "private"], default="private")
    madd.add_argument("--kind", choices=["chat", "embedding", "rerank"], default="chat")
    madd.add_argument("--price", default="", help="输入,缓存命中,输出（元 / 百万 token）")
    _actor_args(madd)

    ck = sub.add_parser("client-key").add_subparsers(dest="action", required=True)
    ensure = ck.add_parser("ensure", help="建一把客户端 Key（已有就沿用），可以同时分配模型")
    ensure.add_argument("--tenant", required=True)
    ensure.add_argument("--type", required=True, help="客户端类型（Agent、Service …）")
    ensure.add_argument("--code", required=True, help="客户端编码")
    ensure.add_argument("--name", default="", help="展示用的名字")
    ensure.add_argument("--models", default="", help="分配的模型，逗号分隔")
    ensure.add_argument("--if-pending", action="store_true", help="已经分配过模型的不覆盖")
    _actor_args(ensure)

    smoke = sub.add_parser("smoke", help="用一把客户端 Key 经网关调一次模型")
    smoke.add_argument("--tenant", required=True)
    smoke.add_argument("--client", required=True, help="客户端类型:编码，如 Service:session-title")
    smoke.add_argument("--model", required=True)
    smoke.add_argument("--url", default="http://127.0.0.1:8091", help="模型网关地址")
    smoke.add_argument("--timeout", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    commands: dict[str, Callable[[argparse.Namespace], int]] = {
        "account": account_command,
        "model": model_command,
        "client-key": client_key_command,
        "encrypt-secrets": encrypt_secrets_command,
        "refresh-keys": refresh_keys_command,
        "smoke": smoke_command,
    }
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "gateway":
            gateway()
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
