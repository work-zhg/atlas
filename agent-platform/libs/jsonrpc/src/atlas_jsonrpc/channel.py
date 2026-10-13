"""MessageChannel —— 按「一条消息」收发文本的双向通道。

stdio（每行一条）与 WebSocket（每帧一条）都实现它，``RpcEndpoint`` 因此与传输无关。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["MessageChannel"]


class MessageChannel(Protocol):
    async def send(self, text: str) -> None:
        """发送一条消息。通道已关闭时抛 ``ChannelClosed``。"""
        ...

    async def receive(self) -> str | None:
        """接收下一条消息；通道已关闭时返回 None。"""
        ...
