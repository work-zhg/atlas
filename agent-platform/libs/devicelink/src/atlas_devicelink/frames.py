"""控制连接上的帧（c4-model/agent/devicegw/code-devicegw-link.html · Frame）。

一条控制连接上跑多路：每路一个通道号。两种帧：

    控制帧（文本，JSON）  {"t": "open", "ch": 1, "kind": "session", "head": {...}}
                          {"t": "close", "ch": 1, "code": 1000, "reason": "..."}
                          {"t": "credit", "ch": 1, "n": 65536}
    数据帧（二进制）      4 字节通道号（大端）+ 1 字节标志 + 内容
                          标志 bit0 = 内容是 UTF-8 文本（会话流里的 atlas.host.v1 消息）

★ 内容原样转交，不在这里解析：会话流里的协议由 Agent 服务和 bridge 两端处理。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "Close",
    "Credit",
    "Data",
    "Frame",
    "FrameError",
    "Open",
    "decode",
    "encode",
]

_HEAD = struct.Struct(">IB")
_TEXT = 0x01
#: 通道号的上限（4 字节无符号）
MAX_CHANNEL = 0xFFFF_FFFF


class FrameError(ValueError):
    """对端发来的帧不合格式。"""


@dataclass(frozen=True)
class Open:
    ch: int
    kind: str
    head: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Close:
    ch: int
    code: int
    reason: str = ""


@dataclass(frozen=True)
class Credit:
    ch: int
    n: int


@dataclass(frozen=True)
class Data:
    ch: int
    payload: str | bytes


Frame = Open | Close | Credit | Data


def encode(frame: Frame) -> str | bytes:
    """帧 → 线上的一条 WebSocket 消息（控制帧是文本，数据帧是二进制）。"""
    match frame:
        case Data(ch=ch, payload=payload):
            if isinstance(payload, str):
                return _HEAD.pack(ch, _TEXT) + payload.encode()
            return _HEAD.pack(ch, 0) + payload
        case Open(ch=ch, kind=kind, head=head):
            return json.dumps(
                {"t": "open", "ch": ch, "kind": kind, "head": head}, ensure_ascii=False
            )
        case Close(ch=ch, code=code, reason=reason):
            return json.dumps(
                {"t": "close", "ch": ch, "code": code, "reason": reason}, ensure_ascii=False
            )
        case Credit(ch=ch, n=n):
            return json.dumps({"t": "credit", "ch": ch, "n": n})
    raise TypeError(f"不认识的帧：{frame!r}")


def decode(message: str | bytes) -> Frame:
    """线上的一条 WebSocket 消息 → 帧。格式不对抛 FrameError。"""
    if isinstance(message, bytes):
        if len(message) < _HEAD.size:
            raise FrameError("数据帧太短")
        ch, flags = _HEAD.unpack_from(message)
        body = message[_HEAD.size :]
        if flags & _TEXT:
            try:
                return Data(ch, body.decode())
            except UnicodeDecodeError as exc:
                raise FrameError("文本数据帧不是 UTF-8") from exc
        return Data(ch, body)
    try:
        obj = json.loads(message)
    except json.JSONDecodeError as exc:
        raise FrameError("控制帧不是 JSON") from exc
    if not isinstance(obj, dict):
        raise FrameError("控制帧必须是对象")
    kind: Literal["open", "close", "credit"] | Any = obj.get("t")
    ch = obj.get("ch")
    if not isinstance(ch, int) or not 0 < ch <= MAX_CHANNEL:
        raise FrameError(f"通道号不合法：{ch!r}")
    if kind == "open":
        head = obj.get("head") or {}
        if not isinstance(obj.get("kind"), str) or not isinstance(head, dict):
            raise FrameError("open 帧要有 kind 和对象形式的 head")
        return Open(ch, obj["kind"], head)
    if kind == "close":
        code = obj.get("code")
        if not isinstance(code, int):
            raise FrameError("close 帧要有整数 code")
        return Close(ch, code, str(obj.get("reason") or ""))
    if kind == "credit":
        n = obj.get("n")
        if not isinstance(n, int) or n <= 0:
            raise FrameError("credit 帧要有正整数 n")
        return Credit(ch, n)
    raise FrameError(f"不认识的控制帧：{kind!r}")
