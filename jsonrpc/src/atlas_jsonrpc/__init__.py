"""Atlas JSON-RPC 2.0：帧、通道协议与双向端点。

与 ACP、上游协议都无关（代码设计 §1.3：本包不得导入任何 atlas_* 包）。
"""

from .channel import MessageChannel
from .endpoint import NotificationHandler, RequestHandler, RpcEndpoint, current_request_id
from .errors import ChannelClosed, ErrorCode, RpcFault
from .frames import (
    Frame,
    InvalidFrame,
    Notification,
    Request,
    RequestId,
    Response,
    encode,
    parse_frame,
)

__all__ = [
    "ChannelClosed",
    "ErrorCode",
    "Frame",
    "InvalidFrame",
    "MessageChannel",
    "Notification",
    "NotificationHandler",
    "Request",
    "RequestHandler",
    "RequestId",
    "Response",
    "RpcEndpoint",
    "RpcFault",
    "current_request_id",
    "encode",
    "parse_frame",
]
