"""python -m atlas_sandboxd serve | barrier"""

from __future__ import annotations

import logging
import signal
import sys

from .barrier import BarrierClient, serve_barrier
from .config import BarrierConfig, ServeConfig
from .execs import ExecManager
from .server import make_server


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mode = argv[1] if len(argv) > 1 else "serve"
    if mode == "barrier":
        serve_barrier(BarrierConfig.from_env())
        return 0
    if mode != "serve":
        print(f"用法：{argv[0]} serve | barrier", file=sys.stderr)
        return 2

    cfg = ServeConfig.from_env()
    manager = ExecManager(cfg, barrier=BarrierClient(cfg.barrier_url) if cfg.barrier_url else None)
    server = make_server(cfg, manager)

    def _stop(*_: object) -> None:
        # 主进程（PID 1）收到 SIGTERM：命令的进程组随 Pod 一起结束，这里只停止接受请求
        manager.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    logging.getLogger(__name__).info(
        "sandboxd %s 监听 %s:%d，工作区 %s", manager.boot_id[:8], cfg.host, cfg.port, cfg.workspace
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
