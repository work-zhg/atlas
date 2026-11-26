"""运营接口的调用方凭据：每个系统一把运营密钥，请求用它签名（运营中心签，业务系统验）。

    签名原文 = 方法 \\n 路径（含查询串）\\n 时间戳 \\n 请求体的 SHA-256 \\n 运营头
    运营头   = OperatorStamp 的四个头（操作人、操作人名字、操作、幂等键），按固定顺序「名:值」逐行，
               没带的值为空
    签名     = HMAC-SHA256(运营密钥, 签名原文)，十六进制

★ 用签名而不是明文令牌：日志或抓包里看到的请求重放不了（时间戳在 5 分钟窗口外就拒），
  一把密钥泄露只影响一个系统。窗口内的重放无害：创建、删除本身就能重做。
★ 运营头必须进签名原文：业务系统按操作人写日志、和运营中心的审计对上。不签的话，截到一个请求
  就能在窗口内改掉操作人。
★ 签名和验证放在同一处，运营中心和各业务系统用同一份实现，不会各写一套对不上。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from .stamp import IDEMPOTENCY, OPERATION, OPERATOR, OPERATOR_NAME, OperatorStamp

__all__ = ["SIGNATURE", "TIMESTAMP", "SignatureError", "sign", "verify"]

TIMESTAMP = "X-Ops-Timestamp"
SIGNATURE = "X-Ops-Signature"

#: 时间戳允许的偏差（秒），两个方向都算
WINDOW = 300


class SignatureError(Exception):
    """签名不对、过期或缺失。"""


#: 进签名原文的运营头，顺序固定
_SIGNED_HEADERS = (OPERATOR, OPERATOR_NAME, OPERATION, IDEMPOTENCY)


def _header(headers: Mapping[str, str], name: str) -> str:
    return headers.get(name) or headers.get(name.lower()) or ""


def _canonical(
    method: str, path: str, timestamp: str, body: bytes, headers: Mapping[str, str]
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    stamped = "\n".join(f"{name.lower()}:{_header(headers, name)}" for name in _SIGNED_HEADERS)
    return f"{method.upper()}\n{path}\n{timestamp}\n{digest}\n{stamped}".encode()


def sign(
    key: bytes,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    stamp: OperatorStamp | None = None,
    at: int | None = None,
) -> dict[str, str]:
    """→ 要加到请求上的头：时间戳、签名，以及 stamp 的运营头（它们一起签）。

    path 要和服务端看到的一致：含查询串，不含主机。
    """
    stamped = stamp.to_headers() if stamp is not None else {}
    timestamp = str(int(time.time() if at is None else at))
    canonical = _canonical(method, path, timestamp, body, stamped)
    mac = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {**stamped, TIMESTAMP: timestamp, SIGNATURE: mac}


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
    canonical = _canonical(method, path, timestamp, body, headers)
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise SignatureError("签名不对")
