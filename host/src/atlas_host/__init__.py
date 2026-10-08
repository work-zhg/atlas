"""Atlas 上游协议：server ⇄ bridge 之间的消息、错误码、关闭码与时限（Bridge 设计 §4）。

★ 本包不得导入 atlas_acp（代码设计 §1.3）：上游协议运送 ACP 内容，但不理解它。
"""

from . import methods
from ._model import JsonObject, Model
from .codes import (
    HEADER_SESSION,
    SUBPROTOCOL_V1,
    SUBPROTOCOLS,
    CloseCode,
    HostErrorCode,
    should_reconnect,
)
from .limits import DEFAULT_LIMITS, TurnLimits
from .messages import *  # noqa: F403
from .messages import __all__ as _messages_all
from .outcomes import (
    CancelCause,
    Cancelled,
    Completed,
    ErrorInfo,
    FailCause,
    Failed,
    Outcome,
    SessionState,
    TurnState,
)

__all__ = [
    "DEFAULT_LIMITS",
    "HEADER_SESSION",
    "SUBPROTOCOLS",
    "SUBPROTOCOL_V1",
    "CancelCause",
    "Cancelled",
    "CloseCode",
    "Completed",
    "ErrorInfo",
    "FailCause",
    "Failed",
    "HostErrorCode",
    "JsonObject",
    "Model",
    "Outcome",
    "SessionState",
    "TurnLimits",
    "TurnState",
    "methods",
    "should_reconnect",
    *_messages_all,
]
