"""能力与参数（c4-model/modelgateway/code-gateway-capability.html）。

模型之间的差异分两层：能做什么（ModelCaps，对调用方的承诺）、同一件事怎么写（UpstreamRules，
网关自带）。这里先看模型做不做得到，做不到就拒绝；做得到就按选中线路的上游模型的写法，
算出 ParamPlan。★ 只出决定，不碰请求：改请求是格式领域读写器的事。
"""

from __future__ import annotations

import hashlib

from .kernel import (
    ErrorCategory,
    GatewayError,
    Intent,
    ModelCaps,
    ModelKind,
    ParamPlan,
    RequestSummary,
    Route,
)
from .presets import EFFORT_LEVELS, PresetRules, UpstreamRules

__all__ = ["CapabilityCheck", "CapabilityGate", "ParamPolicy", "ThinkingProvenance"]

#: 估算的输入超过上下文多少倍才拒绝：估算偏粗，宁可放过也不误拒
CONTEXT_SLACK = 1.2
#: 只支持固定额度思考、调用方又没给额度时用的默认额度
DEFAULT_THINKING_BUDGET = 16_000
#: Anthropic 要求思考额度至少 1024
MIN_THINKING_BUDGET = 1_024

_KIND_TEXT = {ModelKind.CHAT: "对话", ModelKind.EMBEDDING: "向量", ModelKind.RERANK: "重排"}


class CapabilityCheck:
    """去掉会让结果出错的，一律拒绝：模型看不到图也会照样回答，调用方发现不了。"""

    @staticmethod
    def violations(model: str, caps: ModelCaps, summary: RequestSummary) -> list[str]:
        found: list[str] = []
        if caps.kind is not summary.kind:
            found.append(
                f"{model} 是{_KIND_TEXT[caps.kind]}模型，不能用来做{_KIND_TEXT[summary.kind]}"
            )
            return found
        if summary.has_images and not caps.vision:
            found.append(f"{model} 不能看图")
        if summary.has_tools and not caps.tools:
            found.append(f"{model} 不能调用工具")
        if (
            caps.context_window
            and summary.approx_input_tokens > caps.context_window * CONTEXT_SLACK
        ):
            found.append(
                f"输入大约 {summary.approx_input_tokens} token，超过 {model} 的上下文 "
                f"{caps.context_window}"
            )
        return found


class ThinkingProvenance:
    """思考块的来源标记：标上来自哪个上游模型，换了上游就把历史里的思考块去掉。"""

    @staticmethod
    def stamp(route: Route) -> str:
        raw = f"{route.account.provider}/{route.upstream_model}".encode()
        return hashlib.sha256(raw).hexdigest()[:12]


class ParamPolicy:
    """参数的两种处理：去掉（不影响回答对不对的）、换写法（同一个意思这个上游有别的写法）。"""

    @staticmethod
    def decide(
        intent: Intent,
        rules: UpstreamRules,
        caps: ModelCaps,
        route: Route,
    ) -> ParamPlan:
        notes: list[str] = []
        drop: set[str] = set()

        limit = min((x for x in (rules.max_output, caps.max_output) if x), default=None)
        max_output = intent.max_output
        if max_output and limit and max_output > limit:
            notes.append(f"最大输出 {max_output} 超过上限 {limit}，改成 {limit}")
            max_output = limit

        temperature = intent.temperature
        if not rules.accepts_temperature:
            if temperature is not None:
                notes.append("这个上游模型不收温度，去掉")
            temperature = None
            drop |= {"top_p", "top_k"}

        effort = intent.effort
        if effort is not None and effort not in rules.effort_levels:
            notes.append(f"这个上游模型不支持思考力度 {effort}，去掉")
            effort = None

        thinking, budget = None, None
        modes = rules.thinking_modes
        match intent.thinking:
            case None:
                pass
            case "off":
                if "off" in modes:
                    thinking = "off"
                    if (
                        effort
                        and rules.off_max_effort
                        and EFFORT_LEVELS.index(effort) > EFFORT_LEVELS.index(rules.off_max_effort)
                    ):
                        notes.append(
                            f"关闭思考时力度最高 {rules.off_max_effort}，从 {effort} 降下来"
                        )
                        effort = rules.off_max_effort
                else:
                    notes.append("这个上游模型的思考关不掉（或不支持思考参数），不传")
            case want:
                if want == "auto" and "adaptive" in modes:
                    thinking = "adaptive"
                elif "budget" in modes:
                    budget = intent.thinking_budget or DEFAULT_THINKING_BUDGET
                    if max_output and budget >= max_output:
                        budget = max_output - 1
                    if budget >= MIN_THINKING_BUDGET:
                        thinking = "budget"
                    else:
                        notes.append("最大输出太小，放不下思考额度，思考参数不传")
                        budget = None
                elif "adaptive" in modes:
                    thinking = "adaptive"
                else:
                    notes.append("这个上游模型不支持思考参数，不传")

        return ParamPlan(
            upstream_model=route.upstream_model,
            thinking=thinking,
            thinking_budget=budget,
            effort=effort,
            temperature=temperature,
            max_output=max_output,
            drop_fields=frozenset(drop),
            strip_history_thinking=rules.history_thinking == "strip",
            stamp=ThinkingProvenance.stamp(route),
            notes=tuple(notes),
        )


class CapabilityGate:
    def __init__(self, presets: PresetRules | None = None) -> None:
        self.presets = presets or PresetRules()

    def check(self, model: str, caps: ModelCaps, summary: RequestSummary) -> None:
        """选线路之前：请求要的这个模型有没有承诺，没有就拒绝，不打到供应商。"""
        found = CapabilityCheck.violations(model, caps, summary)
        if found:
            raise GatewayError(ErrorCategory.BEYOND_CAPABILITY, "；".join(found))

    def plan(self, caps: ModelCaps, route: Route, summary: RequestSummary) -> ParamPlan:
        """选好线路之后：按这条线路上游模型的写法，把意图算成「参数怎么改」。"""
        rules = self.presets.rules_for(route.account.provider, route.upstream_model)
        return ParamPolicy.decide(summary.intent, rules, caps, route)
