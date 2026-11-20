"""口令、临时密码与会话令牌。

★ 明文密码只出现在一次响应里：不写日志、不写审计、不落库。会话令牌只存哈希。
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = [
    "check_policy",
    "hash_password",
    "hash_token",
    "new_token",
    "temp_password",
    "verify_password",
]

_hasher = PasswordHasher()
#: 去掉易混淆字符 0/O、1/l/I
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(hashed: str, raw: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_policy(raw: str, min_len: int) -> list[str]:
    """返回没满足的要求；空列表 = 通过。运营账号权限大：长度、大小写、数字都要。"""
    errs: list[str] = []
    if len(raw) < min_len:
        errs.append(f"至少 {min_len} 位")
    if len(raw) > 128:
        errs.append("最多 128 位")
    if not any(c.isupper() for c in raw) or not any(c.islower() for c in raw):
        errs.append("含大写和小写字母")
    if not any(c.isdigit() for c in raw):
        errs.append("含数字")
    return errs


def temp_password() -> str:
    """临时密码：16 位，满足口令规则（首次登录必须改）。"""
    while True:
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
        if not check_policy(raw, 10):
            return raw


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
