"""NotifyChannel：即时推送的渠道（扩展点）。加渠道只加实现，通知器不改。

这一版只有扩展点：企业微信、钉钉、邮件汇总要配了凭据才有实现（没配就不推，站内通知照常）。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["NotifyChannel"]


class NotifyChannel(Protocol):
    #: wecom / dingtalk / email
    code: str

    async def send(self, account: str, message: str, link: str) -> None:
        """给一个账号发一条消息。失败抛异常：通知器只记日志，不挡站内通知。"""
