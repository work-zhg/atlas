"""配置服务的入口与运维命令。

    python -m atlas_config serve                       起 HTTP 服务（默认 :8020）
    python -m atlas_config migrate                     升级配置库到最新
    python -m atlas_config skill publish <目录|zip>    发布技能（P1 的发布入口）
    python -m atlas_config skill approve|reject <upload_id> --note …
    python -m atlas_config skill disable|enable|revoke|rescan <slug> <version> [--reason …]
    python -m atlas_config skill adopt <slug> <version> 登记源仓库里已有的技能
    python -m atlas_config skill list
    python -m atlas_config mcp import-env [--json …]  从 MCP_SERVERS 导入注册表

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

from .db.session import get_engine, get_sessionmaker
from .errors import ConfigError
from .mcp.service import McpRegistry
from .settings import get_settings
from .skill.package import ScanBlocked, load_dir, load_zip
from .skill.service import SkillService, limits_of
from .storage import make_storage

logger = logging.getLogger("atlas_config")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


# ───────────────────────────────────────────── serve / migrate


def _port() -> int:
    """★ 不用 ATLAS_CONFIG_PORT：Service 名叫 atlas-config 时，kubelet 会注入
    ATLAS_CONFIG_PORT=tcp://… 这种遗留变量（cluster 服务踩过同一个坑）。"""
    raw = os.environ.get("ATLAS_CONFIG_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8020


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    settings = get_settings()
    if not settings.internal_token:
        logger.warning(
            "ATLAS_CONFIG_INTERNAL_TOKEN 未设置：/internal/* 不校验服务间令牌 —— 仅限本机开发"
        )
    uvicorn.run(
        create_app(cors_origins=settings.cors_origins),
        host=os.environ.get("ATLAS_CONFIG_HOST", "0.0.0.0"),
        port=_port(),
    )


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)


# ───────────────────────────────────────────── skill / mcp


def _actor(args: argparse.Namespace) -> str:
    return args.actor or os.environ.get("ATLAS_CONFIG_ACTOR") or getpass.getuser()


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


async def _with_registry(fn: Callable[[McpRegistry], Awaitable[Any]]) -> Any:
    try:
        async with get_sessionmaker()() as session:
            result = await fn(McpRegistry(session=session))
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
                "\n待人工审查（含脚本或非 builtin）。审查后执行：\n"
                f"  python -m atlas_config skill approve {row.id} --reviewer <审查人> --note <意见>"
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


def _mcp_servers_json(explicit: str | None) -> str:
    if explicit:
        return explicit
    if raw := os.environ.get("MCP_SERVERS"):
        return raw
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "MCP_SERVERS":
                return value.strip().strip("'\"")
    raise SystemExit("找不到 MCP_SERVERS（环境变量或仓库根 .env），也没有给 --json")


def mcp_command(args: argparse.Namespace) -> int:
    raw = _mcp_servers_json(args.json)
    created = asyncio.run(_with_registry(lambda reg: reg.import_env(raw, actor=_actor(args))))
    print("已导入：" + (", ".join(created) if created else "（无，全部已存在）"))
    return 0


# ───────────────────────────────────────────── argparse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_config")
    parser.add_argument("--actor", help="操作人（默认当前系统用户）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")

    skill = sub.add_parser("skill").add_subparsers(dest="action", required=True)
    publish = skill.add_parser("publish")
    publish.add_argument("path")
    publish.add_argument("--source", default="builtin", choices=["builtin", "tenant", "imported"])
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

    mcp = sub.add_parser("mcp").add_subparsers(dest="action", required=True)
    imp = mcp.add_parser("import-env")
    imp.add_argument("--json", help="MCP_SERVERS 的 JSON；不给则读环境变量 / 仓库 .env")
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
        if args.command == "mcp":
            return mcp_command(args)
    except ScanBlocked as exc:
        print(f"✗ {exc.layer} 检查未通过：", file=sys.stderr)
        for hit in exc.hits:
            print(f"  - {hit}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
