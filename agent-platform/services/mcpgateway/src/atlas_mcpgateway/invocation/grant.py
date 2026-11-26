"""IdentityGrant：Atlas 打开 CLI 会话时签发的身份凭据（c4-model/mcpgateway/code-invocation.html）。

    凭据 = base64url(JSON {t: 会话, a: 按谁判, u: 发起人, tenant: 租户})
           + "." + HMAC-SHA256 十六进制（密钥和 Atlas 共享，settings.identity_grant_secret）

CLI 放在请求头 X-Identity-Grant 里带来。网关验签后，给上游写 X-Auth-Uid = 凭据的按谁判、
X-Uid = 凭据的发起人（invocation.service.CallerIdentity）。

★ 为什么要它：会话 Pod 跑的是不可信代码，X-Auth-Uid 它想写什么都行。按谁判取决于智能体的权限类型
  （以 Agent 为权限是数字员工，以人为权限是使用者），只有 Atlas 知道，所以由 Atlas 签进凭据。
★ 只说明身份，不授予服务：能用哪些服务、哪些工具仍只按 Key 判（一个来源）。
★ 租户必须和 Key 所在的租户一致：拿别的租户的凭据配自己的 Key 不行。
★ 不设过期：CLI 会话只在打开时下发一次配置，可能挂起很久再接着用。
★ 与 Atlas 的签发（atlas_agent/providers/mcp/grant.py）各自实现，算法写在这里的说明里。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from uuid import UUID

__all__ = ["GRANT_HEADER", "GrantInvalid", "IdentityGrant", "sign", "verify"]

GRANT_HEADER = "x-identity-grant"


class GrantInvalid(Exception):
    """签名不对、内容不全、租户和 Key 的对不上。"""


@dataclass(frozen=True)
class IdentityGrant:
    #: 这次 CLI 会话（会话 uuid）：给上游作 X-Thread-Id（调用方没带时）
    thread: str
    #: 按谁判：这一轮的 auth-uid（以 Agent 为权限时是数字员工）
    auth_uid: str
    #: 谁在用：只作审计
    uid: str
    tenant: UUID


def _digest(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def sign(secret: str, *, thread: str, auth_uid: str, uid: str, tenant: UUID) -> str:
    """签一张凭据（Atlas 那边的签发是同一个算法；这里给测试和排查用）。"""
    payload = json.dumps(
        {"t": thread, "a": auth_uid, "u": uid, "tenant": str(tenant)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_digest(secret, body)}"


def verify(secret: str, token: str, *, tenant: UUID) -> IdentityGrant:
    """验签、取内容、核对租户。→ IdentityGrant；不对抛 GrantInvalid。"""
    if not secret:
        raise GrantInvalid("网关没有配置身份凭据的密钥，不认身份凭据")
    body, _, sig = token.strip().partition(".")
    if not body or not sig:
        raise GrantInvalid("身份凭据格式不对")
    if not hmac.compare_digest(sig, _digest(secret, body)):
        raise GrantInvalid("身份凭据签名不对")
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        grant = IdentityGrant(
            thread=str(data.get("t") or ""),
            auth_uid=str(data["a"]),
            uid=str(data.get("u") or data["a"]),
            tenant=UUID(str(data["tenant"])),
        )
    except (binascii.Error, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GrantInvalid("身份凭据内容不全") from exc
    if not grant.auth_uid:
        raise GrantInvalid("身份凭据里没有按谁判")
    if grant.tenant != tenant:
        raise GrantInvalid("身份凭据的租户和 Key 所在的租户不一致")
    return grant
