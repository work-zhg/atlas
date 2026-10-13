"""上游协议的 12 个方法（Bridge 设计 §4.4 · §9.3）。

方法名用点号，与 ACP 的斜杠形式（``session/prompt``）明显区分（§4.1 P5）。
"""

from __future__ import annotations

__all__ = [
    "AGENT_UPDATE",
    "BRIDGE_TO_SERVER",
    "PERMISSION_ASK",
    "PERMISSION_WITHDRAW",
    "SERVER_TO_BRIDGE",
    "SESSION_ACK",
    "SESSION_ATTACH",
    "SESSION_CLOSE",
    "SESSION_ENDED",
    "SESSION_OPEN",
    "SESSION_STATE",
    "TURN_CANCEL",
    "TURN_START",
    "TURN_STATE",
]

# server → bridge
SESSION_OPEN = "session.open"  # 请求
SESSION_ATTACH = "session.attach"  # 请求
SESSION_ACK = "session.ack"  # 通知
SESSION_CLOSE = "session.close"  # 请求
TURN_START = "turn.start"  # 请求
TURN_CANCEL = "turn.cancel"  # 请求

# bridge → server（全部带 seq）
SESSION_STATE = "session.state"  # 通知
SESSION_ENDED = "session.ended"  # 通知
TURN_STATE = "turn.state"  # 通知
PERMISSION_ASK = "permission.ask"  # 请求
PERMISSION_WITHDRAW = "permission.withdraw"  # 通知
AGENT_UPDATE = "agent.update"  # 通知

SERVER_TO_BRIDGE = frozenset(
    {SESSION_OPEN, SESSION_ATTACH, SESSION_ACK, SESSION_CLOSE, TURN_START, TURN_CANCEL}
)
BRIDGE_TO_SERVER = frozenset(
    {SESSION_STATE, SESSION_ENDED, TURN_STATE, PERMISSION_ASK, PERMISSION_WITHDRAW, AGENT_UPDATE}
)
