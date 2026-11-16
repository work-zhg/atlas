"""Atlas 设备连接：连接器 ⇄ 设备网关之间一条连接上的多路复用（会话流、画面流、文件流，
以及开关接管窗口这类浏览器请求）。

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
#: 本机浏览器的固定请求（开、关接管窗口，剪贴板，状态）：一问一答，和控制帧一样最先发
BROWSER = "browser"
#: 画面流：浏览器画面和键鼠（VNC 的原始字节），排在会话流之后、文件流之前
VIEW = "view"

__all__ = [
    "BROWSER",
    "FILE",
    "GOING_AWAY",
    "INTERNAL_ERROR",
    "NORMAL",
    "PROTOCOL_VIOLATION",
    "SESSION",
    "VIEW",
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
