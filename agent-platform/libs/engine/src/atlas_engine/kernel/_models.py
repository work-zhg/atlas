"""Shared helpers for resolving and inspecting chat models."""

from __future__ import annotations

import logging

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


def resolve_model(model: str | BaseChatModel) -> BaseChatModel:
    """Resolve a model string to a `BaseChatModel`.

    If `model` is already a `BaseChatModel`, returns it unchanged.

    String models are resolved via `init_chat_model`, composed with any
    provider-specific initialization behavior registered in the
    `ProviderProfile` registry. Built-in registrations supply NVIDIA NIM and
    OpenRouter app attribution headers plus the OpenAI Responses API default;
    users can layer additional providers or overrides via
    `register_provider_profile`.

    Args:
        model: Model string (e.g. `"openai:gpt-5.4"`) or pre-configured
            `BaseChatModel` subclass instance.

    Returns:
        Resolved `BaseChatModel` instance.
    """
    if isinstance(model, BaseChatModel):
        return model

    # ★ ProviderProfile 已删：它只管「构造 chat model 时塞什么参数」，
    #   而 atlas 走的是 model_factory.build_chat_model —— 自己 new 一个
    #   实例再传进来，上面那个 isinstance 分支直接 return，这条路从不经过。
    #   接 LLM 网关之后更没意义：只有一个端点、一种协议、一套凭据，
    #   provider 维度在构造层已经被网关抹平了。
    return init_chat_model(model)

