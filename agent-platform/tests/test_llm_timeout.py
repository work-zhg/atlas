"""单次模型调用的超时必须真的传给 SDK（litellm_timeout_s）。

早先只在配置里声明、没传：实际用 SDK 默认的 600 秒，网关让流卡住时一轮干等 10 分钟
（2026-10-13 智能体评测：两个 trial 卡满预算、轨迹为空）。
"""

from __future__ import annotations

from atlas_agent.config import get_settings
from atlas_agent.domain.spec import ModelSpec
from atlas_agent.providers.llm.factory import build_chat_model


def test_anthropic_client_gets_request_timeout() -> None:
    m = build_chat_model(
        ModelSpec(model="deepseek-chat", provider="anthropic"), base_url="http://x", api_key="k"
    )
    assert m.default_request_timeout == get_settings().litellm_timeout_s
    assert m.max_retries == 3
