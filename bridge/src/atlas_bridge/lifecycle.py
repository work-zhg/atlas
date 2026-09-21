"""优雅终止 —— Pod 收到 SIGTERM 后的三件事。

    ① 拒绝新请求       已在跑的那一轮让它跑完
    ② 当前 run 标中断并上报   否则会话永远停在「运行中」
    ③ 刷 OTel span     否则这一轮在链路图上凭空消失

★ terminationGracePeriodSeconds 要留够这三件事（执行环境 §09）。留不够的
  后果不是慢，是**静默丢事件**：Pod 被 SIGKILL 时上报还没发出去。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from atlas_acp.wire import Method

from atlas_bridge.adapter import AdapterProcess
from atlas_bridge.ws import BridgeServer

logger = logging.getLogger(__name__)

__all__ = ["install_signal_handlers", "shutdown"]


async def shutdown(server: BridgeServer, adapter: AdapterProcess, *, grace_s: float = 10.0) -> None:
    """三步终止。任一步失败都继续往下 —— 收尾路径上不许因为一个异常就
    把后面的步骤跳过（那正是「事件凭空消失」的来源）。"""
    server.begin_drain()  # ①

    # ② 让 server 知道这一轮没跑完。用通知而不是请求：对端可能已经走了，
    #    等响应只会把 grace 窗口耗光。
    with contextlib.suppress(Exception):
        await server.on_adapter_notification(
            {
                "jsonrpc": "2.0",
                "method": Method.BRIDGE_ADAPTER_CRASHED,
                "params": {"reason": "bridge 正在终止（Pod 关闭）", "graceful": True},
            }
        )

    with contextlib.suppress(Exception):
        async with asyncio.timeout(grace_s):
            await adapter.stop()

    # ③ OTel 的 flush 在接入遥测后补在这里（执行环境 §09）。
    logger.info("bridge 终止完成")


def install_signal_handlers(
    server: BridgeServer, adapter: AdapterProcess, *, grace_s: float = 10.0
) -> None:
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        loop.create_task(shutdown(server, adapter, grace_s=grace_s))

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal)
