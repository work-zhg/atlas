"""本进程的实例 id（doc/horizontal-scaling-design.html §4）。

run 租约的 owner、维护者选主、记忆队列的处理中列表都用它。

★ 进程级唯一且整个进程生命周期不变：同一个 id 的两个进程会互相续对方的租约，
  一个死了另一个也看不出来。默认值带随机后缀，同一台机器上重启也不会撞。
"""

from __future__ import annotations

import os
import secrets
import socket
from functools import lru_cache

from .config import get_settings


@lru_cache
def instance_id() -> str:
    configured = get_settings().instance_id.strip()
    if configured:
        return configured
    host = socket.gethostname() or "atlas"
    return f"{host}-{os.getpid()}-{secrets.token_hex(3)}"[:64]
