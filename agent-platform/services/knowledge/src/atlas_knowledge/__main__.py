"""知识库服务的入口。

python -m atlas_knowledge serve      起 HTTP 服务（默认 :8050）
python -m atlas_knowledge worker     起 Celery worker（队列 dataset）
python -m atlas_knowledge migrate    升级知识库库到最新
python -m atlas_knowledge sync-uc    一次性：把旧的可见范围 / 成员写进用户中心（库级权限接入）
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

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


def sync_uc(service_owner: str | None = None) -> None:
    import asyncio

    from .auth import use_identity
    from .db.session import get_engine, get_sessionmaker
    from .perms import make_data_permissions
    from .sync_uc import sync_uc as run

    async def _go() -> dict[str, int]:
        # 调用户中心的身份：给了接收人就以他的名义（租户也按他推）；否则单租户部署取唯一租户
        use_identity(service_owner)
        perms = make_data_permissions(get_settings())
        try:
            async with get_sessionmaker()() as session:
                return await run(session, perms, service_owner=service_owner)
        finally:
            if hasattr(perms, "aclose"):
                await perms.aclose()
            await get_engine().dispose()

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
        sync_uc(args.service_owner)
    elif args.command == "worker":
        worker(args.concurrency, args.queues)


if __name__ == "__main__":
    main()
