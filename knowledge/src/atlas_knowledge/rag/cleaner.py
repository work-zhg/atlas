"""清洗（参考 Dify core/rag/cleaner/clean_processor.py）。

总是做：去控制字符与非法标记；按规则可选：
  remove_extra_spaces —— 3 个以上换行压成 2 个，连续空白压成 1 个
  remove_urls_emails  —— 去邮箱与 URL，但保留 Markdown 链接与图片
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["clean"]

_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
_MD_IMAGE = re.compile(r"!\[.*?\]\((https?://[^)]+)\)")
_EMAIL = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
_URL = re.compile(r"https?://\S+")
_SPACES = re.compile(r"[\t\f\r\x20\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]{2,}")


def _enabled(rules: dict[str, Any], rule_id: str) -> bool:
    for rule in rules.get("pre_processing_rules") or ():
        if rule.get("id") == rule_id:
            return bool(rule.get("enabled"))
    return False


def clean(text: str, rules: dict[str, Any] | None) -> str:
    text = text.replace("<|", "<").replace("|>", ">")
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = text.replace("\ufffe", "")
    rules = rules or {}

    if _enabled(rules, "remove_extra_spaces"):
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = _SPACES.sub(" ", text)

    if _enabled(rules, "remove_urls_emails"):
        kept: list[str] = []

        def _keep(m: re.Match[str]) -> str:
            kept.append(m.group(0))
            return f"\x00{len(kept) - 1}\x00"

        # 先保护 Markdown 链接与图片（链接文字本身也可能是 URL）
        text = _MD_IMAGE.sub(_keep, text)
        text = _MD_LINK.sub(_keep, text)
        text = _EMAIL.sub("", text)
        text = _URL.sub("", text)
        for i, original in enumerate(kept):
            text = text.replace(f"\x00{i}\x00", original)
    return text
