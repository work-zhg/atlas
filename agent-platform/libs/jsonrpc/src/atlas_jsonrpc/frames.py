"""JSON-RPC 2.0 帧：解析与编码。

只判断「信封」的形状（请求、通知、响应），不理解 params 与 result 的内容 ——
那是 ACP 模型与上游协议各自的事。

★ 编码结果永远是单行：ACP 的 stdio 传输以换行分隔消息，消息内部不得含换行。
  ``json.dumps`` 在紧凑模式下会把字符串里的换行转义成 ``\\n``，不会输出真正的换行。

★ 不支持批量（JSON 数组）。ACP v1 没有批量；v2 虽然允许，但明确建议
  initialize / session/new / session/prompt 这类生命周期消息不要批量发送。
  收到批量时按规范回一个 Invalid Request。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, RpcFault

__all__ = [
    "Frame",
    "InvalidFrame",
    "Notification",
    "Request",
    "RequestId",
    "Response",
    "encode",
    "parse_frame",
]

RequestId = int | str

_VERSION = "2.0"


@dataclass(frozen=True, slots=True)
class Request:
    id: RequestId
    method: str
    params: Any = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: Any = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Response:
    """对某个请求的回答：``error`` 为 None 即成功。"""

    id: RequestId | None
    result: Any = None
    error: RpcFault | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


Frame = Request | Notification | Response


class InvalidFrame(Exception):
    """收到的文本不是一个合法的 JSON-RPC 帧。

    ``request_id`` 是能从坏帧里认出来的 id（认不出就是 None）—— 回错误响应时要用它，
    这样对方才知道是哪个请求出了问题。
    """

    def __init__(
        self, code: int, message: str, request_id: RequestId | None = None, *, reply: bool = True
    ) -> None:
        super().__init__(message)
        self.fault = RpcFault(code, message)
        self.request_id = request_id
        #: 要不要回错误响应。形状像「响应」的坏帧不回：回了对方也只能丢弃，
        #: 而两个都这么做的端点可能来回互发错误。
        self.reply = reply


def _is_id(value: Any) -> bool:
    # bool 是 int 的子类，要单独排除
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def parse_frame(text: str | bytes) -> Frame:
    """解析一条消息。失败时抛 ``InvalidFrame``。"""
    try:
        obj = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidFrame(ErrorCode.PARSE_ERROR, f"不是合法的 JSON：{exc}") from exc

    if isinstance(obj, list):
        raise InvalidFrame(ErrorCode.INVALID_REQUEST, "不支持批量消息")
    if not isinstance(obj, dict):
        raise InvalidFrame(ErrorCode.INVALID_REQUEST, "消息必须是 JSON 对象")

    raw_id = obj.get("id")
    known_id = raw_id if _is_id(raw_id) else None
    if obj.get("jsonrpc") != _VERSION:
        raise InvalidFrame(ErrorCode.INVALID_REQUEST, 'jsonrpc 字段必须是 "2.0"', known_id)

    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str) or not method:
            raise InvalidFrame(ErrorCode.INVALID_REQUEST, "method 必须是非空字符串", known_id)
        params = obj.get("params", {})
        if not isinstance(params, dict | list):
            raise InvalidFrame(ErrorCode.INVALID_REQUEST, "params 必须是对象或数组", known_id)
        if "id" not in obj:
            return Notification(method, params)
        if not _is_id(raw_id):
            raise InvalidFrame(ErrorCode.INVALID_REQUEST, "id 必须是整数或字符串")
        return Request(raw_id, method, params)

    # 没有 method：只能是响应
    if "id" not in obj:
        raise InvalidFrame(ErrorCode.INVALID_REQUEST, "既不是请求也不是响应")
    if raw_id is not None and not _is_id(raw_id):
        raise InvalidFrame(ErrorCode.INVALID_REQUEST, "id 必须是整数、字符串或 null", reply=False)
    has_result, has_error = "result" in obj, "error" in obj
    if has_result == has_error:
        raise InvalidFrame(
            ErrorCode.INVALID_REQUEST,
            "响应必须恰好包含 result 与 error 之一",
            known_id,
            reply=False,
        )
    if has_error:
        err = obj["error"]
        if not isinstance(err, dict) or not isinstance(err.get("code"), int):
            raise InvalidFrame(
                ErrorCode.INVALID_REQUEST, "error 必须是带整数 code 的对象", known_id, reply=False
            )
        fault = RpcFault(err["code"], str(err.get("message", "")), err.get("data"))
        return Response(raw_id, error=fault)
    return Response(raw_id, result=obj["result"])


def encode(frame: Frame) -> str:
    """编码成单行 JSON 文本。"""
    out: dict[str, Any] = {"jsonrpc": _VERSION}
    match frame:
        case Request(id=rid, method=method, params=params):
            out |= {"id": rid, "method": method, "params": params}
        case Notification(method=method, params=params):
            out |= {"method": method, "params": params}
        case Response(id=rid, result=result, error=error):
            out["id"] = rid
            if error is not None:
                out["error"] = error.to_json()
            else:
                out["result"] = result
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
