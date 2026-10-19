"""遥测里的内容：档位判断、凭据脱敏、截断（langfuse-integration-design §9）。

★ 所有写进 span 的内容都必须过这里。档位不够就返回 None，调用方据此不写属性 ——
  off 档时 span 上一个内容字段都不出现，而不是写一个空串。
★ 脱敏在 server 端做而不是 Collector：Collector 的 core 版没有 redaction / transform
  处理器（换 contrib 要多 500MB），而正则放在这里可以写单元测试。
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["allows", "level", "redact", "render"]

_ORDER = {"off": 0, "io": 1, "full": 2}

#: 常见凭据格式。命中的部分替换成 [REDACTED]，保留前缀便于辨认是哪一类。
_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM 私钥整段
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED PRIVATE KEY]",
    ),
    # OpenAI / Anthropic / Langfuse 等 sk-… / pk-lf-… 形式的密钥
    (re.compile(r"\b(sk|pk)-[A-Za-z0-9_\-]{16,}"), r"\1-[REDACTED]"),
    # AWS Access Key
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    # Authorization: Bearer …
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{16,}"), r"\1 [REDACTED]"),
    # key=value 形式：保留键名，只抹值
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|password|passwd|token)[A-Za-z0-9_]*)"
            r"(\s*[:=]\s*)([\"']?)[^\s\"',;]{8,}\3"
        ),
        r"\1\2\3[REDACTED]\3",
    ),
)


def level() -> str:
    """当前档位。★ 读不到配置时按 off：遥测是旁路，不能因为它让 run 失败。"""
    try:
        from ..config import get_settings

        return get_settings().otel_capture_content
    except Exception:
        return "off"


def allows(needed: str) -> bool:
    """当前档位是否达到 needed（io / full）。"""
    return _ORDER.get(level(), 0) >= _ORDER[needed]


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def render(value: Any, needed: str) -> str | None:
    """内容 → 可以写进 span 的字符串。档位不够返回 None（调用方据此不写属性）。

    非字符串先序列化成 JSON（OTel 属性只支持标量与标量数组）；再脱敏；再截断。
    """
    if not allows(needed) or value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = redact(text)
    limit = _max_chars()
    if len(text) > limit:
        text = f"{text[:limit]}…[truncated {len(text) - limit} chars]"
    return text


def _max_chars() -> int:
    try:
        from ..config import get_settings

        return get_settings().otel_content_max_chars
    except Exception:
        return 32_768
