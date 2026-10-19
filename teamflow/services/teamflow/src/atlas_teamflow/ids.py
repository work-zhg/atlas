"""逻辑主键：UUIDv7（按时间有序，对索引友好；总体设计 §11）。

★ Python 3.13 的 uuid 模块还没有 uuid7（3.14 才有），按 RFC 9562 自己生成：
  48 位毫秒时间戳 + 版本 7 + 74 位随机数。
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["uuid7"]


def uuid7() -> uuid.UUID:
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)
