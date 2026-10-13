"""BridgeApp：装配全部组件，按启动顺序运行（代码设计 §6.3）。

  1. 读配置、读 token 文件、生成 bridge 实例 id
  2. 启动 UpstreamServer（此时 /readyz 返回 503）
  3. HostSession.boot()：拉起 agent、完成 initialize（预热）
  4. 进入 IDLE，/readyz 返回 200
  5. 等待；会话结束（session.close / session.ended）后以 4410 关闭连接；收到 SIGTERM 时退出

SIGTERM 的完整排空流程（§8.7）在安全与可观测性一步实现；目前收到信号即结束会话并退出。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
from importlib.metadata import PackageNotFoundError, version

from atlas_host import BridgeInfo, CloseCode

from .agent.process import AgentLaunch
from .agent.supervisor import AgentSupervisor
from .clock import SystemClock
from .config import BridgeConfig
from .errors import AgentUnavailable
from .session.host import HostSession
from .upstream.outbox import Outbox
from .upstream.server import UpstreamServer

__all__ = ["BridgeApp"]

logger = logging.getLogger(__name__)


def _version() -> str:
    try:
        return version("atlas-bridge")
    except PackageNotFoundError:
        return "0.0.0"


class BridgeApp:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.instance = f"b-{secrets.token_hex(6)}"
        clock = SystemClock()
        launch = AgentLaunch(
            argv=config.agent_cmd,
            cwd=config.workspace,
            env=config.agent_env(os.environ),
            user=config.agent_uid,
            group=config.agent_gid,
            oom_score_adj=config.agent_oom_score_adj,
            max_line_bytes=config.max_message_bytes,
        )
        self.agent = AgentSupervisor(
            launch,
            clock,
            boot_timeout_s=config.boot_timeout_s,
            kill_grace_s=config.kill_grace_s,
            client_version=_version(),
        )
        self.outbox = Outbox(max_bytes=config.outbox_bytes)
        self.host = HostSession(
            self.agent,
            self.outbox,
            clock,
            bridge=BridgeInfo(version=_version(), instance=self.instance),
            workspace=str(config.workspace),
            open_timeout_s=config.open_timeout_s,
            request_timeout_s=config.acp_request_timeout_s,
            cancel_grace_s=config.cancel_grace_s,
            ledger_size=config.ledger_size,
            max_pending_asks=config.max_pending_asks,
        )
        self.server = UpstreamServer(
            self.host,
            self.outbox,
            token=config.read_token(),
            session_id=config.session_id,
            listen_host=config.listen_host,
            listen_port=config.listen_port,
            max_message_bytes=config.max_message_bytes,
        )
        self.started = asyncio.Event()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """请求退出（与 SIGTERM 相同）。"""
        self._stop.set()

    async def run(self) -> int:
        """运行到会话结束或收到终止信号。返回进程退出码。"""
        stop = self._stop
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)

        await self.server.start()
        pump = asyncio.create_task(self.outbox.pump(), name="outbox-pump")
        watchdog = asyncio.create_task(self._watch_outbox(), name="outbox-watchdog")
        logger.info("bridge %s 启动，会话 %s", self.instance, self.config.session_id)
        self.started.set()
        try:
            try:
                await self.host.boot()
            except AgentUnavailable as exc:
                logger.error("agent 预热失败：%s", exc)
                return 1

            ended = asyncio.create_task(self.host.ended.wait())
            stopping = asyncio.create_task(stop.wait())
            await asyncio.wait({ended, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if self.host.ended.is_set() and not stop.is_set():
                # session.close 的连接已由连接自己关闭；这里负责 session.ended 的情形。
                # ★ 会话结束后进程不退出：退出会让容器被重启，而这个 Pod 只等回收（§5.5）。
                #   之后的连接一律以 4410 关闭。
                await self.server.close_current_after_drain(CloseCode.SESSION_GONE, "session ended")
                await self.agent.shutdown()
                logger.info("会话已结束，等待 Pod 回收")
                await stopping
            logger.info("收到终止信号，退出")
            ended.cancel()
            return 0
        finally:
            await self.agent.shutdown()
            await self.server.stop(CloseCode.GOING_AWAY, "bridge stopping")
            for task in (pump, watchdog):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _watch_outbox(self) -> None:
        """缓冲持续满：判定上游不健康，以 1011 断开，转入断线处理（淘汰旧数据、等重连）。

        背压未必能让 agent 慢下来（Node 写管道不阻塞，积压只是挪到 agent 的内存里），
        所以需要这条兜底（§6.6）。
        """
        limit = self.config.outbox_full_limit_s
        while True:
            await asyncio.sleep(1.0)
            if self.outbox.full_for() > limit:
                logger.warning("发送缓冲持续满超过 %gs，断开上游连接", limit)
                await self.server.close_current(CloseCode.INTERNAL_ERROR, "outbox full")
