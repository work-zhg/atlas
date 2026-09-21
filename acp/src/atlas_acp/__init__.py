"""ACP 线协议 —— server 与 Pod 内 bridge 共享的唯一接口。

零重依赖（仅 pydantic）：本包会装进 Pod 镜像，多一个依赖就是每个会话 Pod
多一层字节与一个 CVE 面。守卫见 pyproject 的 import-linter contract 与
tests/test_acp_wire.py 的子进程探测。

    wire      JSON-RPC 2.0 帧、方法名、错误码
    types     五个方法的请求与响应
    updates   session/update 的判别联合（translate 的输入）
    caps      adapter 声明的能力位
"""

from atlas_acp.caps import AgentCaps
from atlas_acp.updates import SessionUpdateNotification, parse_update
from atlas_acp.wire import ErrorCode, Method, Notification, Request, Response, parse_frame

__all__ = [
    "AgentCaps",
    "ErrorCode",
    "Method",
    "Notification",
    "Request",
    "Response",
    "SessionUpdateNotification",
    "parse_frame",
    "parse_update",
]
