"""和标准写法不一样的供应商：只覆盖不一样的那几个方法。

★ 各家的写法以各家文档和实测为准；发现新的差异就在这里加，别的领域不用动。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..gateway.kernel import FailureKind, Format, ParamPlan, Usage
from .dialect import StandardDialect, _int

__all__ = ["DIALECTS", "DeepSeekDialect", "OpenAIDialect", "QwenDialect"]


class DeepSeekDialect(StandardDialect):
    """DeepSeek 的 OpenAI 格式接口把缓存命中放在自己的字段里：

    prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens。按标准字段读，命中是 0，
    费用会按全部未命中算（一次智能体调用能多算 5 倍多）。
    """

    def usage(self, fmt: Format, raw: Mapping[str, Any]) -> Usage:
        if fmt is Format.OPENAI and "prompt_cache_hit_tokens" in raw:
            hit = _int(raw.get("prompt_cache_hit_tokens"))
            prompt = _int(raw.get("prompt_tokens"))
            miss = raw.get("prompt_cache_miss_tokens")
            return Usage(
                input=_int(miss) if miss is not None else max(prompt - hit, 0),
                cache_hit=hit,
                output=_int(raw.get("completion_tokens")),
            )
        return super().usage(fmt, raw)


class OpenAIDialect(StandardDialect):
    """OpenAI 额度用完回的是 429 加 insufficient_quota：意思是余额不足，不是限流。

    当成限流的话，线路每分钟冷却一次、再撞一次；归成余额不足，账号标异常、等充值。
    """

    def _kind(self, status: int | None, kind: str, code: str) -> FailureKind:
        if "insufficient_quota" in (kind, code):
            return FailureKind.NO_BALANCE
        return super()._kind(status, kind, code)


class QwenDialect(StandardDialect):
    """通义千问（OpenAI 兼容接口）开关思考的字段是 enable_thinking，额度是 thinking_budget。"""

    def adjust(self, fmt: Format, body: dict[str, Any], plan: ParamPlan) -> dict[str, Any]:
        if fmt is not Format.OPENAI or plan.thinking is None:
            return body
        body["enable_thinking"] = plan.thinking != "off"
        if plan.thinking == "budget" and plan.thinking_budget:
            body["thinking_budget"] = plan.thinking_budget
        else:
            body.pop("thinking_budget", None)
        return body


#: 供应商 → 方言；没写的用 StandardDialect
DIALECTS: dict[str, type[StandardDialect]] = {
    "deepseek": DeepSeekDialect,
    "openai": OpenAIDialect,
    "azure": OpenAIDialect,
    "dashscope": QwenDialect,
}
