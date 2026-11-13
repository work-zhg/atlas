"""设备网关的入口。

python -m atlas_devicegw serve      起服务（默认 :8095）
"""

from __future__ import annotations

import argparse
import logging


def serve() -> None:
    import uvicorn

    from .app import create_app
    from .settings import get_settings

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.http_port,
        # 会话流里单条消息最大 32 MB（与 bridge 一致）；uvicorn 默认只收 16 MB
        ws_max_size=settings.max_message_bytes + 1024,
        # 连接器每 20 秒 ping 一次；这边也 ping，及时发现电脑睡眠、换网络
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        proxy_headers=True,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m atlas_devicegw")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve()
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
