"""Atlas 设备连接：连接器 ⇄ 设备网关之间一条连接上的多路复用（会话流、文件流）。

两边共用：设备网关开通道，连接器接通道。WebSocket 的收发由两边各自适配成 Transport。
"""

from .frames import Close, Credit, Data, Frame, FrameError, Open, decode, encode
from .mux import (
    GOING_AWAY,
    INTERNAL_ERROR,
    NORMAL,
    PROTOCOL_VIOLATION,
    Channel,
    ChannelClosed,
    ChannelStalled,
    Mux,
    Priority,
    Transport,
)

#: 通道类型
SESSION = "session"
FILE = "file"

__all__ = [
    "FILE",
    "GOING_AWAY",
    "INTERNAL_ERROR",
    "NORMAL",
    "PROTOCOL_VIOLATION",
    "SESSION",
    "Channel",
    "ChannelClosed",
    "ChannelStalled",
    "Close",
    "Credit",
    "Data",
    "Frame",
    "FrameError",
    "Mux",
    "Open",
    "Priority",
    "Transport",
    "decode",
    "encode",
]
