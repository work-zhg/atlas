"""bridge 的异常（代码设计 §9）。

错误码的映射只在上游分派处做一次，这里不出现任何数字错误码。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AcpRequestTimeout",
    "AgentProtocolError",
    "AgentRequestFailed",
    "AgentUnavailable",
    "BridgeError",
    "SessionAlreadyOpen",
    "SessionNotOpen",
    "TurnBusy",
    "TurnNotFound",
]


class BridgeError(Exception):
    """所有 bridge 自定义异常的基类。"""


class AcpRequestTimeout(BridgeError):
    """发给 agent 的 ACP 请求超时。

    ★ 收到它的上层应当把 agent 视为状态未知并重启它（Bridge 设计 §6.5 C4）：
      超时的请求可能仍在 agent 那边处理中，不重启的话后续请求会与它的残留交错。
    """

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"ACP 请求 {method} 超过 {timeout:g}s 未响应")
        self.method = method
        self.timeout = timeout


class AgentRequestFailed(BridgeError):
    """agent 对 ACP 请求回了错误。``acp`` 是 agent 返回的 code / message / data，原样。"""

    def __init__(self, method: str, acp: dict[str, Any]) -> None:
        super().__init__(f"ACP 请求 {method} 失败：{acp.get('message', '')}")
        self.method = method
        self.acp = acp


class AgentProtocolError(BridgeError):
    """agent 的行为违反协议（例如协商出不支持的版本、响应形状不对）。"""


# ─────────────────────────────── 会话层：上游指令被拒绝的原因 ───────────────────────────────


class SessionNotOpen(BridgeError):
    """会话当前不接受这条指令。

    ``cause``：not_opened · recovering · draining · ended · bridge_restarted。
    ``data``：随错误带给 server 的附加字段（例如 bridge_restarted 时的新实例 id）。
    """

    def __init__(self, cause: str, **data: Any) -> None:
        super().__init__(f"会话不可用：{cause}")
        self.cause = cause
        self.data = data


class SessionAlreadyOpen(BridgeError):
    """一个 bridge 进程只服务一个会话（Bridge 设计 §5.2），重复 open 被拒绝。

    带上实例 id 与会话 id：server 据此改用 session.attach 接上这个会话。
    """

    def __init__(self, bridge_instance: str, agent_session_id: str | None) -> None:
        super().__init__("本 bridge 的会话已经打开")
        self.bridge_instance = bridge_instance
        self.agent_session_id = agent_session_id


class TurnBusy(BridgeError):
    """已有一轮在进行（Bridge 设计 §4.6：不排队）。"""

    def __init__(self, current: str) -> None:
        super().__init__(f"已有一轮在进行：{current}")
        self.current = current


class TurnNotFound(BridgeError):
    def __init__(self, turn_id: str) -> None:
        super().__init__(f"没有这一轮：{turn_id}")
        self.turn_id = turn_id


class AgentUnavailable(BridgeError):
    """agent 起不来（预热失败、反复崩溃）。``cause`` 供 server 决定怎么处理。"""

    def __init__(self, cause: str) -> None:
        super().__init__(f"agent 不可用：{cause}")
        self.cause = cause
