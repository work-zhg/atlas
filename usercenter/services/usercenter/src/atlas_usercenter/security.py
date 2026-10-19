"""口令、临时密码、令牌与密钥。

★ 明文密码 / Secret 只出现在一次响应里：不写日志、不写审计、不落库（用户设计 §08）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .settings import UCSettings

__all__ = [
    "check_policy",
    "hash_password",
    "hash_token",
    "new_app_key",
    "new_secret",
    "new_token",
    "policy_text",
    "temp_password",
    "token_equals",
    "verify_password",
]

_hasher = PasswordHasher()
#: 去掉易混淆字符 0/O、1/l/I
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOWER = "abcdefghijkmnpqrstuvwxyz"
_DIGIT = "23456789"
_SYMBOL = "!@#$%^&*"


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(hashed: str, raw: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_policy(raw: str, settings: UCSettings) -> list[str]:
    """返回未满足的要求；空列表 = 通过。"""
    errs: list[str] = []
    if len(raw) < settings.password_min_len:
        errs.append(f"至少 {settings.password_min_len} 位")
    if len(raw) > 128:
        errs.append("最多 128 位")
    if settings.password_need_upper and not any(c.isupper() for c in raw):
        errs.append("含大写字母")
    if settings.password_need_digit and not any(c.isdigit() for c in raw):
        errs.append("含数字")
    if settings.password_need_symbol and all(c.isalnum() for c in raw):
        errs.append("含特殊字符")
    return errs


def policy_text(settings: UCSettings) -> str:
    parts = [f"至少 {settings.password_min_len} 位"]
    if settings.password_need_upper:
        parts.append("含大写字母")
    if settings.password_need_digit:
        parts.append("含数字")
    if settings.password_need_symbol:
        parts.append("含特殊字符")
    return "、".join(parts)


def temp_password(settings: UCSettings) -> str:
    """CSPRNG 生成，长度 max(12, 策略最小长度)，保证满足当前策略。"""
    length = max(12, settings.password_min_len)
    chars = [secrets.choice(_UPPER), secrets.choice(_DIGIT), secrets.choice(_SYMBOL)]
    pool = _UPPER + _LOWER + _DIGIT
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def new_token() -> str:
    """会话 / 接口令牌：32 字节随机，Base64URL。"""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """令牌与 App Secret 本身高熵，存 SHA-256 即可，无需慢哈希（应用接入设计 §07）。"""
    return hashlib.sha256(raw.encode()).hexdigest()


def token_equals(raw: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_token(raw), hashed)


def new_app_key() -> str:
    return "ap_" + secrets.token_hex(8)


def new_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
