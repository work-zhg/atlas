"""运营接口的调用方凭据：每个系统一把运营密钥，请求用它签名（运营中心签，业务系统验）。

    签名原文 = 方法 \\n 路径（含查询串）\\n 时间戳 \\n 请求体的 SHA-256
    签名     = HMAC-SHA256(运营密钥, 签名原文)，十六进制

★ 用签名而不是明文令牌：日志或抓包里看到的请求重放不了（时间戳在 5 分钟窗口外就拒），
  一把密钥泄露只影响一个系统。写操作在窗口内的重放由幂等键挡住。
★ 签名和验证放在同一处，运营中心和各业务系统用同一份实现，不会各写一套对不上。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

__all__ = ["SIGNATURE", "TIMESTAMP", "SignatureError", "sign", "verify"]

TIMESTAMP = "X-Ops-Timestamp"
SIGNATURE = "X-Ops-Signature"

#: 时间戳允许的偏差（秒），两个方向都算
WINDOW = 300


class SignatureError(Exception):
    """签名不对、过期或缺失。"""


def _canonical(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{digest}".encode()


def sign(
    key: bytes, method: str, path: str, body: bytes = b"", *, at: int | None = None
) -> dict[str, str]:
    """→ 要加到请求上的两个头。path 要和服务端看到的一致：含查询串，不含主机。"""
    timestamp = str(int(time.time() if at is None else at))
    mac = hmac.new(key, _canonical(method, path, timestamp, body), hashlib.sha256).hexdigest()
    return {TIMESTAMP: timestamp, SIGNATURE: mac}


def verify(
    key: bytes,
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    now: float | None = None,
    window: int = WINDOW,
) -> None:
    timestamp = headers.get(TIMESTAMP) or headers.get(TIMESTAMP.lower())
    given = headers.get(SIGNATURE) or headers.get(SIGNATURE.lower())
    if not timestamp or not given:
        raise SignatureError("缺少签名")
    try:
        at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("时间戳格式不对") from exc
    if abs((time.time() if now is None else now) - at) > window:
        raise SignatureError("签名已过期")
    expected = hmac.new(key, _canonical(method, path, timestamp, body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise SignatureError("签名不对")
