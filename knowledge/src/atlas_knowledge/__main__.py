"""知识库服务的入口。

python -m atlas_knowledge serve      起 HTTP 服务（默认 :8050）
python -m atlas_knowledge worker     起 Celery worker（队列 dataset）
python -m atlas_knowledge migrate    升级知识库库到最新
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
    if not settings.internal_token:
        logger.warning(
            "ATLAS_KB_INTERNAL_TOKEN 未设置：/internal/* 不校验服务间令牌 —— 仅限本机开发"
        )
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m atlas_knowledge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    w = sub.add_parser("worker")
    w.add_argument("--concurrency", type=int, default=None)
    w.add_argument("--queues", default="dataset")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "migrate":
        migrate()
    elif args.command == "worker":
        worker(args.concurrency, args.queues)


if __name__ == "__main__":
    main()
