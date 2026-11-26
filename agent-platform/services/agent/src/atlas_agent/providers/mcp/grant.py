"""身份凭据（X-Identity-Grant）：打开 CLI 会话时签发，CLI 经 MCP 网关调服务时带上。

    凭据 = base64url(JSON {t: 会话, a: 按谁判, u: 发起人, tenant: 租户})
           + "." + HMAC-SHA256 十六进制（密钥和网关共享，settings.mcp_identity_grant_secret）

网关验签后给上游写 X-Auth-Uid = 按谁判、X-Uid = 发起人、X-Thread-Id = 会话
（atlas_mcpgateway/invocation/grant.py 是验的那一边，算法相同、各自实现）。

★ 为什么要它：会话 Pod 跑的是不可信代码，请求头它想写什么都行。按谁判取决于智能体的权限类型
  （以 Agent 为权限是数字员工，以人为权限是使用者），只有 Atlas 知道，所以签进凭据。
★ 只说明身份，不授予服务：CLI 能用哪些服务、哪些工具仍按智能体的 Key 判。
★ 不设过期：CLI 会话只在打开时下发一次配置，可能挂起很久再接着用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from uuid import UUID

__all__ = ["GRANT_HEADER", "sign_identity_grant"]

GRANT_HEADER = "X-Identity-Grant"


def sign_identity_grant(
    secret: str, *, thread: UUID | str, auth_uid: UUID | str, uid: UUID | str, tenant: UUID | str
) -> str:
    payload = json.dumps(
        {"t": str(thread), "a": str(auth_uid), "u": str(uid), "tenant": str(tenant)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"
