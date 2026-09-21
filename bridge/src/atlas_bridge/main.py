"""Bridge 入口 —— Pod 内的 1 号进程。

    读凭证与会话绑定（Secret / env）
      → 拉起 adapter（stdio）
      → 起 WS 服务，等 server 连上来

★ 启动期任何一步失败都**直接退出非零**，不降级。宁可 Pod 起不来，
  也不要起来一个坏的（执行环境 §09）—— 一个"连上了但 adapter 没拉起来"
  的 Pod，表现是每次 prompt 都超时，排查成本远高于起不来。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from atlas_bridge.adapter import AdapterProcess
from atlas_bridge.lifecycle import install_signal_handlers
from atlas_bridge.ws import BridgeServer

logger = logging.getLogger(__name__)

__all__ = ["build", "main"]

#: 容器内的工作目录 —— 就是会话 OSS 前缀的挂载点（文件系统 §05）。
DEFAULT_CWD = "/workspace"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        # ★ 缺凭证就退出。默认一个空 token 等于谁都能连（执行环境 §09）。
        sys.stderr.write(f"缺少必需的环境变量 {name}\n")
        raise SystemExit(2)
    return value


async def build() -> tuple[BridgeServer, AdapterProcess]:
    """按环境变量装配。拆出来是为了让集成测试能不经 main 直接用。"""
    token = _require("ATLAS_BRIDGE_TOKEN")
    thread_id = _require("ATLAS_THREAD_ID")
    command = os.environ.get("ATLAS_ADAPTER_CMD", "").split()
    if not command:
        sys.stderr.write("缺少必需的环境变量 ATLAS_ADAPTER_CMD\n")
        raise SystemExit(2)

    server: BridgeServer | None = None

    async def on_notification(frame: dict) -> None:
        assert server is not None
        await server.on_adapter_notification(frame)

    async def on_request(frame: dict):
        assert server is not None
        return await server.on_adapter_request(frame)

    adapter = AdapterProcess(
        command,
        on_notification=on_notification,
        on_request=on_request,
        cwd=os.environ.get("ATLAS_ADAPTER_CWD", DEFAULT_CWD),
    )
    server = BridgeServer(adapter, token=token, thread_id=thread_id)
    return server, adapter


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server, adapter = await build()
    await adapter.start()
    install_signal_handlers(server, adapter)

    # ★ 监听 0.0.0.0，且**没有** NetworkPolicy 兜底 —— 集群里不加网络策略是
    #   有意的（adapter 本来就要出网）。所以入口的唯一闸门是握手上的
    #   per-Pod token：K8s 的网络是平的，"在集群里"从来不是安全边界。
    #   早先这行的注释写的是"入口由 NetworkPolicy 限"，那条策略并不存在。
    host = os.environ.get("ATLAS_BRIDGE_HOST", "0.0.0.0")  # noqa: S104
    port = int(os.environ.get("ATLAS_BRIDGE_PORT", "8900"))

    serving = asyncio.create_task(server.serve_forever(host, port), name="bridge-ws")
    crashed = asyncio.create_task(adapter.wait_closed(), name="adapter-watch")
    logger.info("bridge 就绪：%s:%d", host, port)

    done, _ = await asyncio.wait({serving, crashed}, return_when=asyncio.FIRST_COMPLETED)
    if crashed in done:
        # ★ adapter 崩了就退出，让 Pod 的重启策略接管 —— 不在进程内自愈。
        #   自愈会掩盖崩溃频率，且半死的 adapter 状态无法验证。
        reason = crashed.result()
        logger.error("adapter 已退出，bridge 随之终止：%s", reason)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
