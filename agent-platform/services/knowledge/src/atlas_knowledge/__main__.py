"""知识库服务的入口。

python -m atlas_knowledge serve      起 HTTP 服务（默认 :8050）
python -m atlas_knowledge worker     起 Celery worker（队列 dataset）
python -m atlas_knowledge migrate    升级知识库库到最新
python -m atlas_knowledge sync-doc-ok
                                     修数据：按库里的文档状态重写向量库的 doc_ok（去掉审核后跑一次）
"""

from __future__ import annotations

import argparse
import asyncio
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
    """嵌入的公共库的表：运营接口库（只剩版本表）、应用配置（系统参数）；各自的版本表。"""
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


def sync_doc_ok() -> None:
    """修数据（document/doc_ok_sync.py）：按库里的文档状态重写向量库的 doc_ok。"""
    from .db.session import get_sessionmaker
    from .document.doc_ok_sync import sync_doc_ok as sync
    from .runtime import build_runtime

    async def run() -> int:
        rt = build_runtime(get_settings())
        try:
            return await sync(rt, get_sessionmaker())
        finally:
            await rt.aclose()

    logger.info("已按库里的文档状态重写 %d 个知识库的 doc_ok", asyncio.run(run()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m atlas_knowledge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    w = sub.add_parser("worker")
    w.add_argument("--concurrency", type=int, default=None)
    w.add_argument("--queues", default="dataset")
    sub.add_parser("sync-doc-ok", help="按库里的文档状态重写向量库的 doc_ok（去掉审核后跑一次）")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "migrate":
        migrate()
    elif args.command == "worker":
        worker(args.concurrency, args.queues)
    elif args.command == "sync-doc-ok":
        sync_doc_ok()


if __name__ == "__main__":
    main()
