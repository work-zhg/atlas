"""AcpClient：ACP client 的类型化包装（代码设计 §7.3）。

不知道轮次与会话，只做三件事：发 ACP 请求、把 agent 的 update 交给回调、
把 agent 的权限请求交给回调并把结果答复回去。

★ bridge 以 client 身份**不声明** fs / terminal 能力（Bridge 设计 §3.7）：agent 本就在
  Pod 里、能直接访问工作区。agent 若仍发来这类请求，回 -32601。
★ update 的内容以**原始 dict** 交给回调，不经模型往返（代码设计 §3）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from atlas_acp.v1 import AgentCaps, InitializeResponse, NewSessionResponse, methods
from atlas_jsonrpc import ErrorCode, MessageChannel, RpcEndpoint, RpcFault
from pydantic import ValidationError

from ..errors import AcpRequestTimeout, AgentProtocolError, AgentRequestFailed

__all__ = ["AcpClient", "Negotiated", "SessionModes"]

#: 本 bridge 支持的 ACP 主版本
SUPPORTED_PROTOCOL = 1

UpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]
PermissionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class SessionModes:
    """agent 在 session/new · load · resume 的响应里报告的权限模式（ACP session modes）。"""

    current: str
    available: tuple[str, ...]


def _modes_from(raw: Any) -> SessionModes | None:
    """宽松地读 ``modes``：不支持模式的 agent 不给这个字段，形状不对也只是当作不支持。"""
    modes = raw.get("modes") if isinstance(raw, dict) else None
    if not isinstance(modes, dict) or not isinstance(modes.get("currentModeId"), str):
        return None
    available = tuple(
        m["id"]
        for m in modes.get("availableModes") or []
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    )
    return SessionModes(current=modes["currentModeId"], available=available)


@dataclass(frozen=True, slots=True)
class Negotiated:
    """ACP 握手的结果。"""

    protocol_version: int
    caps: AgentCaps
    #: 原样保留，上报给 server（翻译层据此选规则，Bridge 设计 §3.7）
    agent_capabilities: dict[str, Any]
    agent_info: dict[str, Any] | None


class AcpClient:
    def __init__(
        self,
        channel: MessageChannel,
        *,
        on_update: UpdateHandler,
        on_permission: PermissionHandler,
        client_name: str = "atlas-bridge",
        client_version: str = "2.0.0",
    ) -> None:
        self._on_update = on_update
        self._on_permission = on_permission
        self._client_info = {"name": client_name, "version": client_version}
        self._endpoint = RpcEndpoint(
            channel,
            name="agent",
            on_request=self._handle_request,
            on_notification=self._handle_notification,
        )
        #: 每个会话打开时 agent 报告的模式（session_id → SessionModes）
        self._modes: dict[str, SessionModes] = {}

    def modes_of(self, session_id: str) -> SessionModes | None:
        """会话打开（新建 / 恢复）时 agent 报告的模式；agent 不支持模式时为 None。"""
        return self._modes.get(session_id)

    def _remember_modes(self, session_id: str, raw: Any) -> None:
        modes = _modes_from(raw)
        if modes is not None:
            self._modes[session_id] = modes

    @property
    def closed(self) -> bool:
        return self._endpoint.closed

    async def run(self) -> None:
        """读循环：直到 agent 关闭 stdout。"""
        await self._endpoint.run()

    # ------------------------------------------------------------------ 请求

    async def initialize(self, *, timeout: float) -> Negotiated:
        raw = await self._request(
            methods.INITIALIZE,
            {
                "protocolVersion": SUPPORTED_PROTOCOL,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": self._client_info,
            },
            timeout=timeout,
        )
        try:
            resp = InitializeResponse.model_validate(raw)
        except ValidationError as exc:
            raise AgentProtocolError(f"initialize 的响应形状不对：{exc}") from exc
        if resp.protocol_version != SUPPORTED_PROTOCOL:
            # 规范：client 不接受 agent 给出的版本时应当关闭连接
            raise AgentProtocolError(
                f"agent 协商出 ACP 版本 {resp.protocol_version}，"
                f"本 bridge 只支持 {SUPPORTED_PROTOCOL}"
            )
        raw_caps = raw.get("agentCapabilities") if isinstance(raw, dict) else None
        return Negotiated(
            protocol_version=resp.protocol_version,
            caps=AgentCaps.from_model(resp.agent_capabilities),
            agent_capabilities=raw_caps if isinstance(raw_caps, dict) else {},
            agent_info=raw.get("agentInfo") if isinstance(raw.get("agentInfo"), dict) else None,
        )

    async def new_session(
        self, cwd: str, mcp_servers: list[dict[str, Any]], *, timeout: float
    ) -> str:
        raw = await self._request(
            methods.SESSION_NEW, {"cwd": cwd, "mcpServers": mcp_servers}, timeout=timeout
        )
        try:
            session_id = NewSessionResponse.model_validate(raw).session_id
        except ValidationError as exc:
            raise AgentProtocolError(f"session/new 的响应形状不对：{exc}") from exc
        self._remember_modes(session_id, raw)
        return session_id

    async def load_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]], *, timeout: float
    ) -> None:
        """恢复会话并重放历史。

        重放的 update 在响应之前经 ``on_update`` 送达（Bridge 设计 §2.7）。
        """
        raw = await self._request(
            methods.SESSION_LOAD,
            {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers},
            timeout=timeout,
        )
        self._remember_modes(session_id, raw)

    async def resume_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]], *, timeout: float
    ) -> None:
        raw = await self._request(
            methods.SESSION_RESUME,
            {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers},
            timeout=timeout,
        )
        self._remember_modes(session_id, raw)

    async def start_prompt(
        self, session_id: str, prompt: list[dict[str, Any]], *, trace: str | None = None
    ) -> asyncio.Future[Any]:
        """发出 session/prompt，立即返回 Future。

        ★ 没有超时参数：一轮的时限由轮次的计时器负责（Bridge 设计 §6.2），不由请求超时负责。
          Future 的结果是 agent 返回的原始 result；agent 回错误时 Future 以 ``RpcFault`` 失败。
        """
        params: dict[str, Any] = {"sessionId": session_id, "prompt": prompt}
        if trace:
            params["_meta"] = {"traceparent": trace}
        return await self._endpoint.start_request(methods.SESSION_PROMPT, params)

    async def cancel(self, session_id: str) -> None:
        """session/cancel（通知，不等响应）。"""
        await self._endpoint.notify(methods.SESSION_CANCEL, {"sessionId": session_id})

    async def set_mode(self, session_id: str, mode_id: str, *, timeout: float) -> None:
        """session/set_mode。agent 若自行降级（如 auto → acceptEdits），会另发 update 告知。"""
        await self._request(
            methods.SESSION_SET_MODE, {"sessionId": session_id, "modeId": mode_id}, timeout=timeout
        )

    async def close_session(self, session_id: str, *, timeout: float) -> None:
        await self._request(methods.SESSION_CLOSE, {"sessionId": session_id}, timeout=timeout)

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        try:
            return await self._endpoint.request(method, params, timeout=timeout)
        except TimeoutError as exc:
            raise AcpRequestTimeout(method, timeout) from exc
        except RpcFault as fault:
            raise AgentRequestFailed(method, fault.to_json()) from fault

    # ------------------------------------------------------------------ 来自 agent

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method == methods.SESSION_UPDATE and isinstance(params, dict):
            await self._on_update(params)
        # 其它通知（包括不认识的 _ 扩展通知）按规范忽略

    async def _handle_request(self, method: str, params: Any) -> Any:
        if method == methods.SESSION_REQUEST_PERMISSION and isinstance(params, dict):
            return await self._on_permission(params)
        # fs/*、terminal/*、elicitation/* 都没有声明能力；_ 扩展方法也不认识
        raise RpcFault(ErrorCode.METHOD_NOT_FOUND, f"不支持的方法：{method}")
