"""JSON-RPC 2.0 帧与 ACP 方法面。

WS 上跑的就是 ACP 的 JSON-RPC —— Bridge 对 server 是透明管道，加三件本地
事务（握手鉴权、单活跃 prompt、优雅终止）。本模块只管「一帧长什么样」，
方法的语义在 types.py / updates.py。

★ 两端共用同一份模型解析：server 与 bridge 对帧的理解漂移，会在类型层
  当场暴露，而不是等到某次线上 CLI 调用静默丢字段。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "JSONRPC_VERSION",
    "ErrorCode",
    "Method",
    "Notification",
    "Request",
    "Response",
    "RpcError",
    "is_notification",
    "parse_frame",
]

JSONRPC_VERSION = "2.0"


class Method:
    """ACP 方法名常量。

    ★ 方法面刻意收敛为五个请求 + 两类通知（acp 详设 §05）。加方法之前先问：
      它是不是能用现有方法的参数表达？协议面每宽一寸，bridge 与 server
      的版本耦合就紧一分（Pod 在跑时是没法原地升级的）。
    """

    # server → bridge
    INITIALIZE = "initialize"
    SESSION_NEW = "session/new"
    SESSION_LOAD = "session/load"
    SESSION_PROMPT = "session/prompt"
    SESSION_CANCEL = "session/cancel"  # notification，不等响应

    # bridge → server
    SESSION_UPDATE = "session/update"  # notification，流式
    SESSION_REQUEST_PERMISSION = "session/request_permission"  # ★ 唯一反向请求

    #: bridge 自报的进程级故障（非 ACP 标准，Atlas 扩展）。
    #: adapter 崩溃是故障不是拒答 —— translate 把它映射成 retryable 的 run.failed。
    BRIDGE_ADAPTER_CRASHED = "bridge/adapter_crashed"


class ErrorCode:
    """JSON-RPC 标准码 + Atlas 的应用码。

    应用码用 -32000..-32099 这段（JSON-RPC 规范保留给实现定义的服务端错误）。
    """

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    #: 已有活跃 prompt。★ 直接拒绝而不是排队 —— 排队会让会话串行锁的超时
    #:   语义变得不可解释（执行环境 §03）。
    SESSION_BUSY = -32001
    #: session/load 的目标会话在 CLI 侧不存在
    SESSION_NOT_FOUND = -32002
    #: adapter 未声明该能力（如 loadSession）。★ 调用方据此走 lost 降级，
    #:   而不是静默新建一个空会话（Subagent §04）。
    CAPABILITY_UNSUPPORTED = -32003


class RpcError(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int
    message: str
    data: Any = None


class Request(BaseModel):
    """有 id 的请求，必须回响应。"""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    id: int | str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class Notification(BaseModel):
    """无 id 的通知，不回响应。

    ★ 没有 id 正是它与 Request 的唯一区别 —— parse_frame 据此分流。
      给通知回响应会让对端的请求-响应关联表里多出对不上号的条目。
    """

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    """成功或失败二选一 —— result 与 error 不得同时出现。"""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    id: int | str | None
    result: Any = None
    error: RpcError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def is_notification(frame: dict[str, Any]) -> bool:
    """通知 = 有 method 且没有 id。"""
    return "method" in frame and "id" not in frame


def parse_frame(frame: dict[str, Any]) -> Request | Notification | Response:
    """一个 JSON 对象 → 三种帧之一。

    ★ 不做严格的协议校验（extra="allow"、未知字段保留）：协议演进时，
      旧的一端遇到新字段不该把连接弄死。真正要校验的是**语义**，那在
      调用方 —— 比如 translate 对未知 update 类型的处理（acp 详设 §06）。
    """
    if "method" in frame:
        return Notification(**frame) if is_notification(frame) else Request(**frame)
    return Response(**frame)
