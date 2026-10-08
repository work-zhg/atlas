"""上游协议的常量：子协议名、握手头、错误码、关闭码（Bridge 设计 §4.3 · §4.11 · §4.12）。"""

from __future__ import annotations

__all__ = [
    "HEADER_SESSION",
    "SUBPROTOCOLS",
    "SUBPROTOCOL_V1",
    "CloseCode",
    "HostErrorCode",
    "should_reconnect",
]

#: WebSocket 子协议名。只在**不兼容**的变更时递增；兼容的新增通过 features 声明。
SUBPROTOCOL_V1 = "atlas.host.v1"
#: server 按偏好顺序报出的全部版本
SUBPROTOCOLS: tuple[str, ...] = (SUBPROTOCOL_V1,)

#: 握手头：本 Pod 只服务这一个会话
HEADER_SESSION = "X-Atlas-Session"


class HostErrorCode:
    """上游协议自己的错误码（-32000 ~ -32099 区段）。与 ACP 的同区段错误码无关。"""

    SESSION_NOT_OPEN = -32010
    SESSION_ALREADY_OPEN = -32011
    TURN_BUSY = -32012
    TURN_NOT_FOUND = -32013
    AGENT_UNAVAILABLE = -32014
    AGENT_ERROR = -32015
    OPEN_TIMEOUT = -32016


class CloseCode:
    NORMAL = 1000
    GOING_AWAY = 1001  # Pod 正在终止（优雅退出）
    ABNORMAL = 1006  # 保留码：没有关闭握手就断开了（只会被观察到，不会被发送）
    MESSAGE_TOO_BIG = 1009
    INTERNAL_ERROR = 1011  # 内部错误；心跳超时也由库以此关闭
    PROTOCOL_VIOLATION = 4400
    SUPERSEDED = 4409  # 被同一会话的新连接取代
    SESSION_GONE = 4410  # 会话已结束


def should_reconnect(code: int | None) -> bool:
    """关闭码的核心用途：告诉 server 要不要重连（§4.12 · §7.5）。

    被取代（4409）说明已经有别的连接接管；会话已结束（4410）说明没有东西可连。
    其余情况（含 None = 连接异常断开、没有收到关闭码）都应当重连。
    """
    return code not in (CloseCode.SUPERSEDED, CloseCode.SESSION_GONE)
