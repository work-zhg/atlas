"""知识库服务的入口。

python -m atlas_knowledge serve      起 HTTP 服务（默认 :8050）
python -m atlas_knowledge worker     起 Celery worker（队列 dataset）
python -m atlas_knowledge migrate    升级知识库库到最新
python -m atlas_knowledge sync-uc --tenant <租户 uuid>
                                     一次性：把旧的可见范围 / 成员写进用户中心（库级权限接入）

★ 库归租户（多租户）：sync-uc 要写明是哪个租户的库，在它里面处理。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from uuid import UUID

from .settings import get_settings

logger = logging.getLogger("atlas_knowledge")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_KNOWLEDGE_PORT：Service 名叫 atlas-knowledge 时 kubelet 会注入同名遗留变量。"""
    raw = os.environ.get("ATLAS_KB_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8050


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    settings = get_settings()
    if not settings.mcp_secret:
        logger.warning("ATLAS_KB_MCP_SECRET 未设置：会话 Pod 里的知识库 MCP 一律拒绝")
    uvicorn.run(
        create_app(cors_origins=settings.cors_origins),
        host=os.environ.get("ATLAS_KB_HOST", "0.0.0.0"),
        port=_port(),
    )


def worker(concurrency: int | None, queues: str) -> None:
    import multiprocessing

    # ★ 子进程不是 fork 出来的（macOS 默认 spawn；Python 3.14 起 Linux 默认 forkserver）时，
    #   Celery 的 fast_trace 优化没有初始化，任务一进来就 "not enough values to unpack"。
    #   这个变量让子进程按「重新执行过」的路径初始化。
    if multiprocessing.get_start_method() != "fork":
        os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

    from .tasks.celery_app import celery_app

    argv = ["worker", "--pool=prefork", f"--queues={queues}", "--loglevel=INFO"]
    if concurrency:
        argv.append(f"--concurrency={concurrency}")
    celery_app.worker_main(argv)


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)
    asyncio.run(_upgrade_opsapi())


async def _upgrade_opsapi() -> None:
    """嵌入的公共库的表：运营接口（租户状态、运营请求）、应用配置（系统参数）；各自的版本表。"""
    from atlas_appconfig import upgrade as upgrade_appconfig
    from atlas_opsapi import upgrade
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(upgrade)
            await conn.run_sync(upgrade_appconfig)
    finally:
        await engine.dispose()


def sync_uc(tenant: str, service_owner: str | None = None) -> None:
    from atlas_tenancy import TenantScope

    from .auth import use_identity
    from .db.session import get_engine, get_sessionmaker
    from .perms import make_data_permissions
    from .sync_uc import sync_uc as run

    async def _go() -> dict[str, int]:
        # 调用户中心的身份：给了接收人就以他的名义，租户是命令行写明的那个
        use_identity(service_owner, tenant=tenant)
        perms = make_data_permissions(get_settings())
        try:
            async with get_sessionmaker()() as session:
                return await run(session, perms, service_owner=service_owner)
        finally:
            if hasattr(perms, "aclose"):
                await perms.aclose()
            await get_engine().dispose()

    with TenantScope.enter(UUID(tenant)):
        out = asyncio.run(_go())
    print(
        f"已写入 {out['synced']} 个库，跳过（已在用户中心）{out['skipped']} 个，"
        f"认定为服务账号的库 {out['service']} 个，成员写入失败 {out['member_failed']} 条，"
        f"服务账号的库改归 --service-owner {out['adopted']} 个"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m atlas_knowledge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    su = sub.add_parser("sync-uc")
    su.add_argument("--tenant", required=True, help="库所属租户的 uuid")
    su.add_argument(
        "--service-owner",
        default=None,
        help="服务账号建的库改归这个用户中心用户（OWNER），如评测用户 atlas-bench",
    )
    w = sub.add_parser("worker")
    w.add_argument("--concurrency", type=int, default=None)
    w.add_argument("--queues", default="dataset")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "migrate":
        migrate()
    elif args.command == "sync-uc":
        sync_uc(args.tenant, args.service_owner)
    elif args.command == "worker":
        worker(args.concurrency, args.queues)


if __name__ == "__main__":
    main()
