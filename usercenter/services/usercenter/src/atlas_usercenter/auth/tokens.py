"""访问 JWT 与公钥集（c4-model/apigateway/code-auth.html、design-api-gateway.html 原理 2）。

用户中心持有 Ed25519 私钥签访问 JWT，网关只拿公钥本地验签，热路径上不再回调用户中心。
  头：alg=EdDSA、kid（公钥指纹）
  载荷：iss、aud、sub（用户）、tid（租户）、sid（会话，即刷新令牌那一行）、mcp（须先改密）、iat、exp
不放角色、操作码、数据权限：会过时，也会让令牌变大；下游照旧按 X-Uid 向用户中心取授权快照。

★ 换钥：先发布、后使用 —— 新公钥放进 jwt_published_keys，过一个网关刷新周期（5 分钟）再换签名私钥。
★ 没配私钥时每个进程临时生成一把（只适合单副本开发）：重启后旧的访问令牌失效，前端会自动刷新。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..settings import UCSettings

__all__ = ["AccessClaims", "KeyRing", "TokenError", "keyring"]

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """访问令牌不能用。code：TOKEN_EXPIRED | TOKEN_INVALID（和网关一样）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AccessClaims:
    user_uuid: UUID
    tenant_uuid: UUID
    #: 会话（刷新令牌那一行）的 uuid：改密时保留当前会话、登出时找到它
    session_uuid: UUID | None
    must_change_password: bool


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _raw(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def kid_of(pub: Ed25519PublicKey) -> str:
    """kid = 公钥指纹：同一把钥匙在哪都是同一个 kid，不用另外配置。"""
    return "k-" + hashlib.sha256(_raw(pub)).hexdigest()[:16]


class KeyRing:
    def __init__(self, settings: UCSettings) -> None:
        self.settings = settings
        if settings.jwt_signing_key.strip():
            key = serialization.load_pem_private_key(settings.jwt_signing_key.encode(), None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("ATLAS_UC_JWT_SIGNING_KEY 要是 Ed25519 私钥（PKCS#8 PEM）")
        else:
            logger.warning(
                "没配 ATLAS_UC_JWT_SIGNING_KEY：临时生成签名私钥，"
                "只适合单副本开发（重启后令牌失效）"
            )
            key = Ed25519PrivateKey.generate()
        self._signing = key
        self.kid = kid_of(key.public_key())
        self._public: dict[str, Ed25519PublicKey] = {self.kid: key.public_key()}
        for kid, pem in (json.loads(settings.jwt_published_keys or "{}") or {}).items():
            pub = serialization.load_pem_public_key(pem.encode())
            if not isinstance(pub, Ed25519PublicKey):
                raise ValueError(f"jwt_published_keys 里的 {kid} 不是 Ed25519 公钥")
            self._public[kid] = pub

    def issue(
        self,
        user_uuid: UUID,
        tenant_uuid: UUID,
        session_uuid: UUID | None,
        must_change_password: bool,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "sub": str(user_uuid),
            "tid": str(tenant_uuid),
            "mcp": bool(must_change_password),
            "iat": now,
            "exp": now + self.settings.access_minutes * 60,
        }
        if session_uuid is not None:
            claims["sid"] = str(session_uuid)
        return jwt.encode(
            claims, self._signing, algorithm="EdDSA", headers={"kid": self.kid, "typ": "JWT"}
        )

    def verify(self, token: str) -> AccessClaims:
        """用户中心自己的接口验访问令牌（经网关透传的 /auth/*、旧网关的 verify）。"""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            pub = self._public.get(kid) if isinstance(kid, str) else None
            if pub is None:
                raise TokenError("TOKEN_INVALID")
            claims = jwt.decode(
                token,
                pub,
                algorithms=["EdDSA"],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "tid"]},
            )
            sid = claims.get("sid")
            return AccessClaims(
                UUID(claims["sub"]),
                UUID(claims["tid"]),
                UUID(sid) if sid else None,
                claims.get("mcp") is True,
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("TOKEN_EXPIRED") from exc
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise TokenError("TOKEN_INVALID") from exc

    def jwks(self) -> dict[str, Any]:
        return {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "kid": kid,
                    "x": _b64url(_raw(pub)),
                    "use": "sig",
                    "alg": "EdDSA",
                }
                for kid, pub in self._public.items()
            ]
        }


_rings: dict[tuple[str, ...], KeyRing] = {}


def keyring(settings: UCSettings) -> KeyRing:
    """进程内一份（临时生成的私钥也要在整个进程里保持同一把）；相关设置变了换一份。"""
    key = (
        settings.jwt_signing_key,
        settings.jwt_published_keys,
        settings.jwt_issuer,
        settings.jwt_audience,
        str(settings.access_minutes),
    )
    ring = _rings.get(key)
    if ring is None:
        ring = _rings[key] = KeyRing(settings)
    return ring


def new_signing_key() -> tuple[str, str, str]:
    """生成一把签名私钥：(kid, 私钥 PEM, 公钥 PEM)。`python -m atlas_usercenter jwt-keygen` 用。"""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return kid_of(key.public_key()), private, public
