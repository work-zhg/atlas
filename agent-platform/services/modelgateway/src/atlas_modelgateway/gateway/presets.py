"""产品自带的上游模型规则和能力（code-gateway-capability.html 的 PresetRules）。

★ 按「供应商 + 上游模型名」匹配（名字支持通配），不按租户起的模型名查。
★ 只写实测过或文档写明的；查不到的按最保守的规则：只传模型名、消息、最大输出。
★ 随网关版本发布，租户不能改；管理平台加线路、登记模型时用同一份数据带出默认能力并校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Literal

from .kernel import ModelCaps, ModelKind, ThinkingSupport

__all__ = ["CONSERVATIVE", "PRESETS", "Preset", "PresetRules", "UpstreamRules"]

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class UpstreamRules:
    """一个上游模型「同一件事怎么写」。"""

    accepts_temperature: bool = False
    effort_levels: tuple[str, ...] = ()
    #: 思考有哪几种写法：adaptive（自适应）、budget（固定额度）、off（关闭）。空 = 不传思考参数
    thinking_modes: frozenset[str] = frozenset()
    #: 思考关闭时力度最高到多少（再高会 400）
    off_max_effort: str | None = None
    max_output: int | None = None
    #: 历史里同一个上游模型产生的思考块：带回（keep）还是去掉（strip）
    history_thinking: Literal["keep", "strip"] = "strip"


#: 查不到规则时：意图里的思考、力度、温度都不传
CONSERVATIVE = UpstreamRules()


@dataclass(frozen=True)
class Preset:
    #: 供应商名；"*" = 任何供应商（同一个模型经转售平台、LiteLLM 也是这个写法）
    provider: str
    #: 上游模型名的通配
    pattern: str
    rules: UpstreamRules
    caps: ModelCaps = field(default_factory=ModelCaps)


_CLAUDE_5 = UpstreamRules(
    # 实测：opus-5 / sonnet-5 / fable-5 显式传 temperature、top_p 都是 400
    accepts_temperature=False,
    effort_levels=EFFORT_LEVELS,
    thinking_modes=frozenset({"adaptive", "off"}),
    # 实测：thinking 关闭时只有 effort <= high 合法
    off_max_effort="high",
    max_output=128_000,
    history_thinking="keep",
)
_CLAUDE_5_CAPS = ModelCaps(
    vision=True,
    tools=True,
    thinking=ThinkingSupport.OPTIONAL,
    context_window=1_000_000,
    max_output=128_000,
)

PRESETS: tuple[Preset, ...] = (
    Preset("*", "claude-opus-5*", _CLAUDE_5, _CLAUDE_5_CAPS),
    Preset("*", "claude-sonnet-5*", _CLAUDE_5, _CLAUDE_5_CAPS),
    # 实测：fable-5 思考恒开，显式传 disabled 是 400
    Preset(
        "*",
        "claude-fable-5*",
        UpstreamRules(
            effort_levels=EFFORT_LEVELS,
            thinking_modes=frozenset({"adaptive"}),
            max_output=128_000,
            history_thinking="keep",
        ),
        ModelCaps(
            vision=True,
            tools=True,
            thinking=ThinkingSupport.ALWAYS_ON,
            context_window=1_000_000,
            max_output=128_000,
        ),
    ),
    # 实测：haiku-4-5 收 temperature；不认 adaptive、不认 effort，只有固定额度的思考
    Preset(
        "*",
        "claude-haiku-4-5*",
        UpstreamRules(
            accepts_temperature=True,
            thinking_modes=frozenset({"budget", "off"}),
            max_output=64_000,
            history_thinking="keep",
        ),
        ModelCaps(
            vision=True,
            tools=True,
            thinking=ThinkingSupport.OPTIONAL,
            context_window=200_000,
            max_output=64_000,
        ),
    ),
    # DeepSeek：默认会思考，显式关闭可用（会话标题就这么用）；「自动」时不传、让它默认思考。
    # 上下文按保守的 64K
    Preset(
        "*",
        "deepseek-*",
        UpstreamRules(
            accepts_temperature=True,
            thinking_modes=frozenset({"off"}),
            history_thinking="keep",
        ),
        ModelCaps(
            tools=True, thinking=ThinkingSupport.OPTIONAL, context_window=64_000, max_output=8_192
        ),
    ),
    # 实测：MiniMax 不接受「关闭思考」，传了直接 400 —— 思考参数一律不传
    Preset(
        "minimax",
        "MiniMax-*",
        UpstreamRules(history_thinking="keep"),
        ModelCaps(tools=True, thinking=ThinkingSupport.ALWAYS_ON),
    ),
    # 通义千问：思考开关是 enable_thinking（方言里改字段名），额度 thinking_budget
    Preset(
        "dashscope",
        "qwen3*",
        UpstreamRules(accepts_temperature=True, thinking_modes=frozenset({"budget", "off"})),
        ModelCaps(tools=True, thinking=ThinkingSupport.OPTIONAL),
    ),
    # 向量、重排：只标类型，参数都不涉及
    Preset("*", "*embedding*", CONSERVATIVE, ModelCaps(kind=ModelKind.EMBEDDING)),
    Preset("*", "*embed*", CONSERVATIVE, ModelCaps(kind=ModelKind.EMBEDDING)),
    Preset("*", "bge-*", CONSERVATIVE, ModelCaps(kind=ModelKind.EMBEDDING)),
    Preset("*", "*rerank*", CONSERVATIVE, ModelCaps(kind=ModelKind.RERANK)),
)


class PresetRules:
    """按「供应商 + 上游模型名」找预置；越具体的越先（指定供应商的优先，通配短的靠后）。"""

    def __init__(self, presets: tuple[Preset, ...] = PRESETS) -> None:
        self._presets = sorted(
            presets, key=lambda p: (p.provider == "*", -len(p.pattern.replace("*", "")))
        )

    def find(self, provider: str, upstream_model: str) -> Preset | None:
        name = upstream_model.lower()
        for p in self._presets:
            if p.provider in ("*", provider) and fnmatchcase(name, p.pattern.lower()):
                return p
        return None

    def rules_for(self, provider: str, upstream_model: str) -> UpstreamRules:
        found = self.find(provider, upstream_model)
        return found.rules if found else CONSERVATIVE

    def caps_for(self, provider: str, upstream_model: str) -> ModelCaps | None:
        found = self.find(provider, upstream_model)
        return found.caps if found else None
