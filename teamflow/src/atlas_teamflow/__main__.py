"""AI TeamFlow 的入口与运维命令。

python -m atlas_teamflow serve          起 HTTP 服务（默认 :8040）
python -m atlas_teamflow migrate        升级 TeamFlow 库
python -m atlas_teamflow worker         独立的 Agent 监督器进程（多实例部署；可起多个）
python -m atlas_teamflow register-uc --account admin [--rotate]
                                        登记操作 / 菜单 / 角色 / 数据编码，输出 App Key / Secret
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from .errors import TFError
from .settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _port() -> int:
    """★ 不用 ATLAS_TF_PORT 一类名字：k8s Service 会注入同名遗留变量。"""
    raw = os.environ.get("ATLAS_TF_HTTP_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8040


def serve() -> None:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=os.environ.get("ATLAS_TF_HOST", "0.0.0.0"), port=_port())


def worker() -> None:
    """独立 worker：只跑 Agent 监督器（投递指令、跟随 Atlas 事件流、提交产物），不接 HTTP。

    ★ 可以起多个，与 API 内嵌的监督器并存都安全：同一节点同一时刻只有拿到租约的一个在处理。
    """
    import asyncio
    import signal

    from .agents.supervisor import AgentSupervisor
    from .api.app import check_deployment
    from .atlas import AtlasClient
    from .db.session import get_engine, get_sessionmaker
    from .process.bus import make_coordinator, set_coordinator
    from .process.git_store import make_git_store

    settings = get_settings()
    check_deployment(settings)
    if not settings.redis_url:
        print(
            "✗ worker 需要 ATLAS_TF_REDIS_URL（单实例开发请直接用 serve，内嵌监督器）",
            file=sys.stderr,
        )
        raise SystemExit(1)

    async def _run() -> None:
        coord = make_coordinator(settings.redis_url, settings.worker_lease_seconds)
        set_coordinator(coord)
        atlas = AtlasClient(settings)
        git = make_git_store(settings)
        await git.prepare()
        sup = AgentSupervisor(
            get_sessionmaker,
            atlas,
            git,
            lease_seconds=settings.worker_lease_seconds,
            sweep_seconds=settings.worker_sweep_seconds,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await coord.start()
        await sup.start()
        logging.getLogger("atlas_teamflow").info(
            "worker 已启动（实例 %s）", getattr(coord, "instance", "-")
        )
        await stop.wait()
        await sup.close()
        await coord.close()
        await atlas.aclose()
        await get_engine().dispose()

    asyncio.run(_run())


def migrate(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, revision)


def register_uc(args: argparse.Namespace) -> int:
    from .register import register

    password = os.environ.get("UC_ADMIN_PASSWORD") or getpass.getpass(
        f"用户中心账号 {args.account} 的密码："
    )
    out = register(
        args.uc_url or get_settings().uc_base_url, args.account, password, rotate=args.rotate
    )
    print(f"\nATLAS_TF_UC_APP_KEY={out['app_key']}")
    if out["app_secret"]:
        print(f"ATLAS_TF_UC_APP_SECRET={out['app_secret']}")
        print("（Secret 只显示这一次，写入 .env）")
    else:
        print("（应用已存在，Secret 未变；需要新 Secret 时加 --rotate）")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m atlas_teamflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("migrate")
    sub.add_parser("worker")
    reg = sub.add_parser("register-uc")
    reg.add_argument(
        "--account", required=True, help="用户中心管理员账号（需 app:manage、role:manage）"
    )
    reg.add_argument("--uc-url", default=None)
    reg.add_argument("--rotate", action="store_true", help="应用已存在时轮换 Secret")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "migrate":
            migrate()
            return 0
        if args.command == "worker":
            worker()
            return 0
        if args.command == "register-uc":
            return register_uc(args)
    except TFError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
