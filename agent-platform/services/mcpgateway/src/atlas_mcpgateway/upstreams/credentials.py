"""上游凭据：网关调上游时注入，调用方拿不到。

    类型        适用          不保密的部分（credential）      保密的值（credential_secret）
    none        全部          —                              —
    apikey      REST          key（名称）、where（header|query）  值
    bearer      MCP、REST     —                              Token
    header      MCP           key（请求头名称）                 值
    basic       REST          user                           密码
    oauth2      MCP、REST     token_url、user（client_id）、scope  client_secret
    metadata    gRPC          key（metadata 键）               值
    mtls        gRPC          cert（客户端证书 PEM）            私钥 PEM

★ OAuth2 客户端凭据：网关用 client_credentials 换 Token，按（地址, client_id, scope）缓存到过期前
  30 秒；各实例各自换。
★ 保存后不回显：接口只给「••••末 4 位」。
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import Invalid

__all__ = ["BY_KIND", "Credential", "TokenCache", "hint", "validate"]

#: 每种服务能用哪些凭据类型
BY_KIND: dict[str, tuple[str, ...]] = {
    "mcp": ("none", "bearer", "header", "oauth2"),
    "rest": ("none", "apikey", "bearer", "basic", "oauth2"),
    "grpc": ("none", "metadata", "mtls"),
}
#: 类型 → 必填的不保密字段
_REQUIRED: dict[str, tuple[str, ...]] = {
    "none": (),
    "apikey": ("key", "where"),
    "bearer": (),
    "header": ("key",),
    "basic": ("user",),
    "oauth2": ("token_url", "user"),
    "metadata": ("key",),
    "mtls": ("cert",),
}
_FIELDS = ("key", "where", "user", "token_url", "scope", "cert")


def hint(secret: str | None) -> str | None:
    return f"••••{secret[-4:]}" if secret else None


def validate(
    kind: str, raw: Mapping[str, Any], secret: str | None, *, previous: Mapping[str, Any] | None
) -> dict[str, Any]:
    """接口给的凭据 → 存库的不保密部分。secret 为 None = 沿用原来的（类型没变时）。"""
    kind_types = BY_KIND[kind]
    ctype = str(raw.get("type") or "none")
    if ctype not in kind_types:
        raise Invalid(f"{kind} 服务不支持凭据类型 {ctype}（可选：{'、'.join(kind_types)}）")
    out: dict[str, Any] = {"type": ctype}
    for f in _FIELDS:
        value = raw.get(f)
        if isinstance(value, str) and value.strip():
            out[f] = value.strip()
    missing = [f for f in _REQUIRED[ctype] if f not in out]
    if missing:
        raise Invalid(f"凭据缺少：{'、'.join(missing)}")
    if ctype == "apikey" and out["where"] not in ("header", "query"):
        raise Invalid("API Key 的位置只能是 header 或 query")
    keeps = previous is not None and previous.get("type") == ctype
    if ctype != "none" and not secret and not keeps:
        raise Invalid("请填写凭据的值")
    return out


@dataclass(frozen=True)
class Credential:
    type: str
    fields: Mapping[str, Any]
    secret: str | None

    @classmethod
    def of(cls, credential: Mapping[str, Any] | None, secret: str | None) -> Credential:
        data = dict(credential or {})
        return cls(str(data.get("type") or "none"), data, secret)

    def get(self, name: str) -> str | None:
        value = self.fields.get(name)
        return str(value) if value else None

    async def http(self, tokens: TokenCache) -> tuple[dict[str, str], dict[str, str]]:
        """HTTP 上游（MCP、REST）：→ (请求头, 查询参数)。"""
        secret = self.secret or ""
        match self.type:
            case "apikey":
                pair = {str(self.get("key")): secret}
                return ({}, pair) if self.get("where") == "query" else (pair, {})
            case "bearer":
                return {"Authorization": f"Bearer {secret}"}, {}
            case "header":
                return {str(self.get("key")): secret}, {}
            case "basic":
                raw = f"{self.get('user')}:{secret}".encode()
                return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}, {}
            case "oauth2":
                token = await tokens.token(
                    str(self.get("token_url")), str(self.get("user")), secret, self.get("scope")
                )
                return {"Authorization": f"Bearer {token}"}, {}
        return {}, {}

    def grpc_metadata(self) -> list[tuple[str, str]]:
        if self.type == "metadata":
            return [(str(self.get("key")).lower(), self.secret or "")]
        return []


class TokenCache:
    """OAuth2 client_credentials 换来的 Token。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._rows: dict[tuple[str, str, str | None], tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def token(self, url: str, client_id: str, secret: str, scope: str | None) -> str:
        key = (url, client_id, scope)
        row = self._rows.get(key)
        if row is not None and row[0] > time.monotonic():
            return row[1]
        async with self._lock:
            row = self._rows.get(key)
            if row is not None and row[0] > time.monotonic():
                return row[1]
            form = {"grant_type": "client_credentials", "client_id": client_id}
            form["client_secret"] = secret
            if scope:
                form["scope"] = scope
            r = await self._http.post(url, data=form, timeout=15.0)
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"换取 OAuth2 Token 失败：HTTP {r.status_code}", request=r.request, response=r
                )
            body = r.json()
            token = str(body["access_token"])
            ttl = float(body.get("expires_in") or 300)
            self._rows[key] = (time.monotonic() + max(ttl - 30, 10), token)
            return token
