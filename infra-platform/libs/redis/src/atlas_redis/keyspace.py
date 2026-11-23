"""强类型键空间构造器（c4-model/infra-platform/code-redis.html）。

统一键形状：

    无 hash_tag：atlas:{service}:{scope}:{domain}:{p1}:{p2}…
    带 hash_tag：atlas:{service}:{scope}:{domain}:{{{hash_tag}}}:{p1}…

其中 scope 在传入 tenant 时为 `t:{tenant}`，否则为 `sys`。

★ 为什么必须有 hash_tag 支持：未来迁移到 Redis Cluster 时，多键 Lua 脚本（如序号键 + 事件流键）
  要求所有 KEYS 落在同一哈希槽，否则报 CROSSSLOT。提前通过 hash_tag 把同组键锚定在同一槽位，
  上集群时不需要改动业务键结构。
★ 为什么单独提供 pattern()：指标与 OTel Span 里绝不能放含 UUID 的原始键名，否则监控基数爆炸。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

__all__ = ["KeySpace"]

_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")
_TAG_BAD_RE = re.compile(r"[\s{}\x00-\x1f]")


def _clean_part(value: str | int | UUID, *, label: str) -> str:
    text = str(value)
    if not text or any(ord(ch) < 0x21 for ch in text):
        raise ValueError(f"Redis 键段 {label} 不能为空或包含空白/控制字符：{text!r}")
    return text


@dataclass(frozen=True)
class KeySpace:
    """一个服务下某个领域的键空间。"""

    service: str
    domain: str
    prefix: str = "atlas"

    def __post_init__(self) -> None:
        for field_name, val in (
            ("prefix", self.prefix),
            ("service", self.service),
            ("domain", self.domain),
        ):
            if not val or not _SEGMENT_RE.match(val):
                raise ValueError(f"KeySpace.{field_name} 格式非法：{val!r}")

    def key(
        self,
        *parts: str | int | UUID,
        tenant: UUID | str | None = None,
        hash_tag: str | UUID | None = None,
    ) -> str:
        """构造完整 Redis 键。"""
        scope = f"t:{_clean_part(tenant, label='tenant')}" if tenant is not None else "sys"
        segments = [self.prefix, self.service, scope, self.domain]
        if hash_tag is not None:
            tag = str(hash_tag)
            if not tag or _TAG_BAD_RE.search(tag):
                raise ValueError(f"hash_tag 不能为空或包含大括号/空白/控制字符：{tag!r}")
            segments.append(f"{{{tag}}}")
        for idx, part in enumerate(parts):
            segments.append(_clean_part(part, label=f"parts[{idx}]"))
        return ":".join(segments)

    def pattern(self, *labels: str, has_tenant: bool = False) -> str:
        """生成低基数脱敏键模式，用于 OTel Span / 慢日志 / 指标标签。"""
        scope = "t:*" if has_tenant else "sys"
        suffix = ":".join(labels) if labels else "*"
        return f"{self.prefix}:{self.service}:{scope}:{self.domain}:{suffix}"

    def channel(self, *parts: str | int | UUID, tenant: UUID | str | None = None) -> str:
        """构造 PubSub 频道名（沿用同一命名空间，避免跨服务频道撞名）。"""
        return self.key(*parts, tenant=tenant)
