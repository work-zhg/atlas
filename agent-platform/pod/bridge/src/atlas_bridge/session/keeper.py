"""让 CLI 会话一直有一个可用的 CLI 进程（code-bridge-session.html · CliKeeper）。

启动时预热（拉起 + 握手）；按 ResumePlanner 的计划新建、恢复会话；CLI 不可用时重启它
（有节制，超过就抛 AgentUnavailable）。会话处于什么阶段、要不要告诉 server 由 SessionHost 决定，
这里只管 CLI 这一侧。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from atlas_host import JsonObject, ResumeSpec
from atlas_jsonrpc import ChannelClosed

from ..cli.client import AcpClient, Negotiated
from ..cli.ports import CliPort
from ..errors import AcpRequestTimeout, AgentRequestFailed
from .modes import ModeGuard
from .resume import OpenMethod, plan_open

__all__ = ["CliKeeper"]

logger = logging.getLogger(__name__)


class CliKeeper:
    def __init__(
        self,
        cli: CliPort,
        modes: ModeGuard,
        *,
        workspace: str,
        open_timeout_s: float,
        request_timeout_s: float,
    ) -> None:
        self._cli = cli
        self._modes = modes
        self._workspace = workspace
        self._open_timeout_s = open_timeout_s
        self._request_timeout_s = request_timeout_s
        #: CLI 进程是否仍可信（取消不响应之后不再向它发 session/close）
        self.healthy = True
        #: 正在用 session/load 重放历史：这期间的内容更新标成 replay
        self.replaying = False
        #: 预热（或重启）结束时 set；open 遇到还在预热就等它
        self.booted = asyncio.Event()
        #: open 时 server 给的 MCP 配置，恢复会话时原样再用一次
        self._mcp_servers: list[JsonObject] = []

    @property
    def negotiated(self) -> Negotiated:
        return self._cli.negotiated

    @property
    def restarts(self) -> int:
        return self._cli.restarts

    @property
    def client(self) -> AcpClient:
        return self._cli.client

    async def boot(self) -> None:
        """预热：拉起 CLI 并完成 initialize。失败抛 AgentUnavailable。"""
        try:
            await self._cli.boot()
        finally:
            self.booted.set()

    async def restart(self, cause: str) -> None:
        """终止 → 按节制等待 → 重新启动。超过节制抛 AgentUnavailable。"""
        self.healthy = False
        self.booted.clear()
        try:
            await self._cli.restart(cause)
        finally:
            self.booted.set()

    async def open_session(
        self, resume: ResumeSpec | None, mcp_servers: list[JsonObject] | None = None
    ) -> tuple[str, bool, Any]:
        """→ (CLI 的会话 id, 恢复成功了吗, 实际重放了什么)。

        按能力位选方法；恢复失败不是错误 —— 新建会话并如实报告 resumed=False（§5.6）。
        mcp_servers 不给就沿用上一次 open 的（恢复会话时原样再用一次）。
        """
        if mcp_servers is not None:
            self._mcp_servers = mcp_servers
        client = self._cli.client
        mcp = self._mcp_servers
        timeout = self._open_timeout_s
        plan = plan_open(self._cli.negotiated.caps, resume.replay if resume else None)

        if resume is not None and plan.method is not OpenMethod.NEW:
            try:
                if plan.method is OpenMethod.LOAD:
                    self.replaying = True
                    await client.load_session(
                        resume.agent_session_id, self._workspace, mcp, timeout=timeout
                    )
                else:
                    await client.resume_session(
                        resume.agent_session_id, self._workspace, mcp, timeout=timeout
                    )
                self._modes.absorb(resume.agent_session_id)
                return resume.agent_session_id, True, plan.replayed
            except AgentRequestFailed as exc:
                logger.warning("恢复会话 %s 失败，改为新建：%s", resume.agent_session_id, exc)
            finally:
                self.replaying = False

        session_id = await client.new_session(self._workspace, mcp, timeout=timeout)
        self._modes.absorb(session_id)
        return session_id, False, "none"

    async def close_session(self, session_id: str) -> None:
        """关掉 CLI 的会话（CLI 支持、且进程还可信时）。失败只记日志：随后就终止进程。"""
        if not self.healthy or not self._cli.negotiated.caps.close:
            return
        try:
            await self._cli.client.close_session(session_id, timeout=self._request_timeout_s)
        except (AgentRequestFailed, AcpRequestTimeout, ChannelClosed) as exc:
            logger.warning("关闭 agent 会话失败：%s", exc)

    async def shutdown(self) -> None:
        await self._cli.shutdown()
