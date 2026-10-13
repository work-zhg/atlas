"""上游方法 → HostSession 调用；异常 → 上游错误码（Bridge 设计 §4.11；代码设计 §9）。

★ 错误码的映射只在这里做一次：session/ 里不出现任何数字错误码。
★ 所有错误的 data 至少带 cause（机器可读）与 detail（给人看的排查信息）。
★ 未预期的异常回 -32603，记完整堆栈；不让它终止事件循环。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from atlas_host import (
    CloseResult,
    HostErrorCode,
    Model,
    OpenParams,
    TurnCancelParams,
    TurnStartParams,
    methods,
)
from atlas_jsonrpc import ErrorCode, RpcFault
from pydantic import ValidationError

from ..errors import (
    AcpRequestTimeout,
    AgentRequestFailed,
    AgentUnavailable,
    BridgeError,
    SessionAlreadyOpen,
    SessionNotOpen,
    TurnBusy,
    TurnNotFound,
)
from ..session.host import HostSession

__all__ = ["dispatch", "to_fault"]

logger = logging.getLogger(__name__)


async def _open(host: HostSession, params: Any) -> Model:
    return await host.open(OpenParams.model_validate(params))


async def _close(host: HostSession, params: Any) -> Model:
    await host.close()
    return CloseResult()


async def _turn_start(host: HostSession, params: Any) -> Model:
    return await host.start_turn(TurnStartParams.model_validate(params))


async def _turn_cancel(host: HostSession, params: Any) -> Model:
    return host.cancel_turn(TurnCancelParams.model_validate(params).turn_id)


_HANDLERS: dict[str, Callable[[HostSession, Any], Awaitable[Model]]] = {
    methods.SESSION_OPEN: _open,
    methods.SESSION_CLOSE: _close,
    methods.TURN_START: _turn_start,
    methods.TURN_CANCEL: _turn_cancel,
}


async def dispatch(host: HostSession, method: str, params: Any) -> dict[str, Any]:
    """处理一个上游请求，返回响应的 result；失败抛 RpcFault。"""
    handler = _HANDLERS.get(method)
    if handler is None:
        raise RpcFault(ErrorCode.METHOD_NOT_FOUND, f"不支持的方法：{method}")
    try:
        result = await handler(host, params)
    except ValidationError as exc:
        raise RpcFault(
            ErrorCode.INVALID_PARAMS,
            "参数不合法",
            {
                "cause": "invalid_params",
                "detail": exc.errors(include_url=False, include_input=False),
            },
        ) from exc
    except BridgeError as exc:
        raise to_fault(method, exc) from exc
    return result.to_wire()


def to_fault(method: str, exc: BridgeError) -> RpcFault:
    detail = str(exc)
    match exc:
        case SessionNotOpen():
            code, data = HostErrorCode.SESSION_NOT_OPEN, {"cause": exc.cause, **exc.data}
        case SessionAlreadyOpen():
            code, data = (
                HostErrorCode.SESSION_ALREADY_OPEN,
                {
                    "cause": "already_open",
                    "bridgeInstance": exc.bridge_instance,
                    "agentSessionId": exc.agent_session_id,
                },
            )
        case TurnBusy():
            code, data = HostErrorCode.TURN_BUSY, {"cause": "turn_busy", "turnId": exc.current}
        case TurnNotFound():
            code, data = HostErrorCode.TURN_NOT_FOUND, {"cause": "turn_not_found"}
        case AgentUnavailable():
            code, data = HostErrorCode.AGENT_UNAVAILABLE, {"cause": exc.cause}
        case AgentRequestFailed():
            code, data = HostErrorCode.AGENT_ERROR, {"cause": "agent_error", "acp": exc.acp}
        case AcpRequestTimeout() if method == methods.SESSION_OPEN:
            code, data = HostErrorCode.OPEN_TIMEOUT, {"cause": "open_timeout"}
        case AcpRequestTimeout():
            code, data = HostErrorCode.AGENT_UNAVAILABLE, {"cause": "agent_timeout"}
        case _:
            logger.error("未映射的 bridge 异常：%r", exc)
            code, data = ErrorCode.INTERNAL_ERROR, {"cause": "internal"}
    data["detail"] = detail
    return RpcFault(code, detail, data)
