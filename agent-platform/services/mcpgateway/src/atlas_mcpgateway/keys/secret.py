"""值对象 KeySecret：Key 原文是 mk- 加 32 字节随机数，页面只显示 mk-…末 4 位。"""

from __future__ import annotations

import secrets

__all__ = ["PREFIX", "generate", "mask"]

PREFIX = "mk-"


def generate() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def mask(secret: str) -> str:
    return f"{PREFIX}…{secret[-4:]}"
