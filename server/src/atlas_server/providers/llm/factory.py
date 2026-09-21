"""ModelSpec → LangChain BaseChatModel（文档 §4.3）。

返回 BaseChatModel 而不是裸 SDK client，是因为 kernel 基于
LangGraph，要的就是这个类型 —— 现在建对，P4 不用重写。

网关约束全部在这里落地，调用方不需要知道哪个模型能传什么（实测见 §3）：
  · temperature：opus-5 / sonnet-5 / fable-5 传了就 400，只有 haiku-4-5 收
  · thinking：用 adaptive，绝不传 budget_tokens
  · effort：langchain-anthropic 0.3.22 没有 output_config 字段，走 model_kwargs
  · base_url：字段名是 anthropic_api_url，不是 base_url
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from .compat import apply_litellm_compat
from atlas_engine.contracts import UnsupportedProvider
from ...domain.spec import ModelSpec

# 必须在构造任何 ChatAnthropic 之前打上（流式路径依赖它）
apply_litellm_compat()


def build_chat_model(
    spec: ModelSpec, *, base_url: str, api_key: str, callbacks: list | None = None
) -> BaseChatModel:
    """callbacks 走构造器，不是事后改属性、也不是 with_config。

    ★ with_config 会把模型包成 RunnableBinding，而图要对它调 bind_tools ——
      包一层之后那条路不可靠。构造器参数是 LangChain 原生支持的，且对
      下游完全透明。
    """
    spec.validate()

    if spec.provider == "anthropic":
        return _build_anthropic(spec, base_url=base_url, api_key=api_key, callbacks=callbacks)
    if spec.provider == "openai":
        return _build_openai(spec, base_url=base_url, api_key=api_key, callbacks=callbacks)
    raise UnsupportedProvider(f"未知 provider: {spec.provider!r}", provider=spec.provider)


def _build_anthropic(
    spec: ModelSpec, *, base_url: str, api_key: str, callbacks: list | None = None
) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, object] = {
        "model": spec.model,
        # ★ 字段名是 anthropic_api_url，不是 base_url（0.3 与 1.5 都如此）
        "anthropic_api_url": base_url,
        "anthropic_api_key": api_key,
        "max_tokens": spec.max_output_tokens,
        # usage.updated 事件要 token 计数，必须开
        "stream_usage": True,
        # §13.1：429 与 5xx 由 SDK 自动指数退避重试；重试用尽后
        # 才会走到 errors.classify 分类成事件。
        "max_retries": 3,
    }

    # ★ langchain-anthropic 1.5 起 output_config 是**原生字段**
    #   （0.3 时没有，只能塞 model_kwargs，那样还会告警）。
    #   不支持的模型（haiku-4-5）必须完全不带该参数，否则 400。
    if (effort := spec.resolve_effort()) is not None:
        kwargs["output_config"] = {"effort": effort}

    # thinking 三态：adaptive / disabled / 完全不发送。
    # haiku-4-5 属于第三种 —— 它不认 adaptive（实测 400），而它正是摘要与
    # 标题生成的默认模型，发错了会在 P4/P5 才炸，离病根很远。
    thinking_mode = spec.resolve_thinking()
    if thinking_mode == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}
    elif thinking_mode == "off":
        kwargs["thinking"] = {"type": "disabled"}

    # ★ 关键：不支持的模型必须**完全不传**该参数。
    #   langchain 的 temperature 默认为 None 且 None 不入 payload —— 实测已验证
    #   （opus-5 在不传时 200，显式传 0.2 时 400）。
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature

    if callbacks:
        kwargs["callbacks"] = callbacks
    return ChatAnthropic(**kwargs)  # type: ignore[arg-type]


def _build_openai(
    spec: ModelSpec, *, base_url: str, api_key: str, callbacks: list | None = None
) -> BaseChatModel:
    """OpenAI 协议分支：thinking / effort / prompt_cache 在此降级为 no-op。"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # 本期未接 OpenAI 协议模型
        raise UnsupportedProvider(
            "provider='openai' 需要 langchain-openai，本期未安装",
            provider="openai",
        ) from exc

    kwargs: dict[str, object] = {
        "model": spec.model,
        "base_url": f"{base_url.rstrip('/')}/v1",
        "api_key": api_key,
        "max_tokens": spec.max_output_tokens,
    }
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature
    if callbacks:
        kwargs["callbacks"] = callbacks
    return ChatOpenAI(**kwargs)  # type: ignore[arg-type]
