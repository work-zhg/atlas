"""技能应用的入口与运维命令。

    python -m atlas_skill serve                       起 HTTP 服务（默认 :8020）
    python -m atlas_skill migrate                     升级技能库到最新
    python -m atlas_skill skill --tenant <租户 uuid> publish <目录|zip>   上传到这个租户（待审查）
    python -m atlas_skill skill --tenant <租户 uuid> approve|reject <upload_id> --note …
    python -m atlas_skill skill --tenant <租户 uuid> disable|enable|revoke|rescan <slug> <version>
    python -m atlas_skill skill --tenant <租户 uuid> adopt <slug> <version>  登记桶根已有的技能
    python -m atlas_skill skill --tenant <租户 uuid> list
    python -m atlas_skill market publish <目录|zip> [--default]   发布到技能市场（产品自带的技能）
    python -m atlas_skill market list
    python -m atlas_skill market install --tenant <租户 uuid> [slug[@版本] …]
                                     给租户装（不写 = 装全部默认安装的）

★ 租户技能的命令都要写明是哪个租户的（多租户）；技能市场不属于任何租户（用户 11-19 定）。

★ 命令行与 HTTP 调同一个 SkillService —— 发布规则只有一份实现。
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
from pathlib import Path
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope

from .db.session import get_engine, get_sessionmaker
from .errors import SkillError
from .settings import get_settings
from .skill.market import MarketService
from .skill.package import ScanBlocked, load_dir, load_zip
from .skill.service import SkillService, limits_of
from .storage import make_storage

logger = logging.getLogger("atlas_skill")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


# ───────────────────────────────────────────── serve / migrate


def _port() -> int:
    """★ 不用 ATLAS_SKILL_PORT：Service 名叫 atlas-skill 时，kubelet 会注入
    ATLAS_SKILL_PORT=tcp://… 这种遗留变量（cluster 服务踩过同一个坑）。"""
    raw = os.environ.get("ATLAS_SKILL_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8020


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    settings = get_settings()
    uvicorn.run(
        create_app(cors_origins=settings.cors_origins),
        host=os.environ.get("ATLAS_SKILL_HOST", "0.0.0.0"),
        port=_port(),
    )


def migrate(revision: str = "head") -> None:
    """本服务的表，再加运营接口库的两张表（租户状态、运营请求；各自的版本表）。"""
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


# ───────────────────────────────────────────── skill


def _actor(args: argparse.Namespace) -> str:
    return args.actor or os.environ.get("ATLAS_SKILL_ACTOR") or getpass.getuser()


async def _with_service(fn: Callable[[SkillService], Awaitable[Any]]) -> Any:
    settings = get_settings()
    storage = make_storage(settings)
    try:
        async with get_sessionmaker()() as session:
            result = await fn(SkillService(session=session, storage=storage, settings=settings))
            await session.commit()
            return result
    finally:
        await get_engine().dispose()


def _print_version(row: Any) -> None:
    content = row.scan_result.get("content", {})
    similar = row.scan_result.get("similarity", {})
    print(f"{row.slug}  状态={row.status}  版本={row.version or '-'}  上传 id={row.id}")
    print(f"  {row.file_count} 个文件 · {row.size_bytes} 字节 · 含脚本={row.has_scripts}")
    print(f"  {row.content_hash}")
    for hit in content.get("hits", [])[:20]:
        print(f"  ⚠ {hit}")
    for hit in similar.get("hits", []):
        print(f"  ⚠ {hit}")


def _load(path: Path) -> Any:
    limits = limits_of(get_settings())
    if path.is_dir():
        return load_dir(path, limits)
    return load_zip(path.read_bytes(), limits)


def skill_command(args: argparse.Namespace) -> int:
    with TenantScope.enter(UUID(args.tenant)):
        return _skill_command(args)


async def _with_market(fn: Callable[[MarketService], Awaitable[Any]]) -> Any:
    settings = get_settings()
    storage = make_storage(settings)
    try:
        async with get_sessionmaker()() as session:
            result = await fn(MarketService(session=session, storage=storage, settings=settings))
            await session.commit()
            return result
    finally:
        await get_engine().dispose()


def market_command(args: argparse.Namespace) -> int:
    actor = _actor(args)
    if args.action == "publish":
        pkg = _load(Path(args.path))
        row = asyncio.run(
            _with_market(
                lambda svc: svc.publish(
                    pkg, actor=actor, install_by_default=True if args.default else None
                )
            )
        )
        print(f"✓ 技能市场：{row.slug} v{row.version}（{row.file_count} 个文件）")
        return 0
    if args.action == "list":
        for item in asyncio.run(_with_market(lambda svc: svc.listing())):
            states = ", ".join(f"v{v.version}:{v.status}" for v in item.versions)
            flag = "默认安装" if item.install_by_default else ""
            print(f"{item.slug:<28} latest={item.latest or '-':<4} {flag:<6} {states}")
        return 0
    with TenantScope.enter(UUID(args.tenant)):
        installed = asyncio.run(
            _with_market(lambda svc: svc.install_many(args.skills, actor=actor))
        )
    for one in installed:
        print(f"{'✓ 装好' if one.copied else '· 已有'} {one.slug} v{one.version}")
    return 0


def _skill_command(args: argparse.Namespace) -> int:
    actor = _actor(args)

    if args.action == "publish":
        pkg = _load(Path(args.path))

        async def _publish(svc: SkillService) -> Any:
            row = await svc.ingest(pkg, source=args.source, actor=actor, origin_url=args.origin_url)
            if row.status == "pending_review" and args.reviewed_by:
                # 命令行的审查 = 代码评审里的那位 reviewer 签字（设计 §6.1）
                row = await svc.review(
                    row.id,
                    reviewer=args.reviewed_by,
                    approve=True,
                    note=args.note or "命令行发布时审查通过",
                )
            return row

        row = asyncio.run(_with_service(_publish))
        _print_version(row)
        if row.status == "pending_review":
            print(
                "\n待本租户的审核人审查。审查后执行：\n"
                f"  python -m atlas_skill skill --tenant {args.tenant} approve {row.id}"
                " --reviewer <审查人> --note <意见>"
            )
        return 0

    if args.action in ("approve", "reject"):
        row = asyncio.run(
            _with_service(
                lambda svc: svc.review(
                    UUID(args.upload_id),
                    reviewer=args.reviewer or actor,
                    approve=args.action == "approve",
                    note=args.note,
                )
            )
        )
        _print_version(row)
        return 0

    if args.action in ("disable", "enable", "revoke"):
        row = asyncio.run(
            _with_service(
                lambda svc: svc.change_status(
                    args.slug, args.version, action=args.action, actor=actor, reason=args.reason
                )
            )
        )
        print(f"{row.slug} v{row.version} → {row.status}")
        return 0

    if args.action == "adopt":
        row = asyncio.run(
            _with_service(lambda svc: svc.adopt(args.slug, args.version, actor=actor))
        )
        _print_version(row)
        return 0

    if args.action == "rescan":
        row = asyncio.run(
            _with_service(lambda svc: svc.rescan(args.slug, args.version, actor=actor))
        )
        print(json.dumps(row.scan_result["history"][-1], ensure_ascii=False, indent=2))
        return 0

    if args.action == "list":
        items = asyncio.run(_with_service(lambda svc: svc.admin_list()))
        for item in items:
            states = ", ".join(
                f"v{v.version}:{v.status}" if v.version else f"{v.status}({str(v.id)[:8]})"
                for v in item.versions
            )
            print(f"{item.slug:<28} {item.source:<9} latest={item.latest or '-':<4} {states}")
        return 0

    raise AssertionError(args.action)


# ───────────────────────────────────────────── argparse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_skill")
    parser.add_argument("--actor", help="操作人（默认当前系统用户）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")

    skill_cmd = sub.add_parser("skill")
    skill_cmd.add_argument("--tenant", required=True, help="技能所属租户的 uuid")
    skill = skill_cmd.add_subparsers(dest="action", required=True)
    publish = skill.add_parser("publish")
    publish.add_argument("path")
    publish.add_argument("--source", default="tenant", choices=["tenant", "imported"])
    publish.add_argument("--origin-url")
    publish.add_argument("--reviewed-by", help="含脚本时必须：审查人（不能是自己）")
    publish.add_argument("--note")
    for name in ("approve", "reject"):
        p = skill.add_parser(name)
        p.add_argument("upload_id")
        p.add_argument("--reviewer")
        p.add_argument("--note", required=True)
    for name in ("disable", "enable", "revoke", "adopt", "rescan"):
        p = skill.add_parser(name)
        p.add_argument("slug")
        p.add_argument("version", type=int)
        if name in ("disable", "enable", "revoke"):
            p.add_argument("--reason", required=name == "revoke")
    skill.add_parser("list")

    market = sub.add_parser("market", help="技能市场（产品自带的技能，不属于任何租户）")
    market_sub = market.add_subparsers(dest="action", required=True)
    mpub = market_sub.add_parser("publish", help="发布一个版本到技能市场")
    mpub.add_argument("path")
    mpub.add_argument("--default", action="store_true", help="同时标成创建租户时默认安装")
    market_sub.add_parser("list")
    minst = market_sub.add_parser("install", help="给租户装（不写 = 装全部默认安装的）")
    minst.add_argument("--tenant", required=True, help="租户 uuid")
    minst.add_argument("skills", nargs="*", help="slug 或 slug@版本")
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
        if args.command == "skill":
            return skill_command(args)
        if args.command == "market":
            return market_command(args)
    except ScanBlocked as exc:
        print(f"✗ {exc.layer} 检查未通过：", file=sys.stderr)
        for hit in exc.hits:
            print(f"  - {hit}", file=sys.stderr)
        return 2
    except SkillError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
