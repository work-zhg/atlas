"""配对凭证的签发（执行环境 §09）。

★ 每 Pod 一份、随 Pod 生灭、不轮换（重建即换新）。
  长期有效的全局令牌落到几百个 Pod 的文件系统上，是个不必要的暴露面。
"""

from __future__ import annotations

import secrets

__all__ = ["issue_token"]

#: 32 字节 URL-safe ≈ 256 bit 熵。它挡的是「同集群其他工作负载扫过来」——
#: K8s 的网络是平的，猜中就等于拿到该会话的工作区与凭证。
_TOKEN_BYTES = 32


def issue_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)
