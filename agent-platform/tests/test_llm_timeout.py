"""单次模型调用的超时必须真的传给 SDK（litellm_timeout_s）。

早先只在配置里声明、没传：实际用 SDK 默认的 600 秒，网关让流卡住时一轮干等 10 分钟
（2026-10-13 智能体评测：两个 trial 卡满预算、轨迹为空）。
"""

from __future__ import annotations

from atlas_agent.config import get_settings
from atlas_agent.executor.run_models import default_model_builder
from atlas_engine.contracts.spec import ModelSpec


def test_anthropic_client_gets_request_timeout() -> None:
    m = default_model_builder(
        ModelSpec(model="deepseek-chat", provider="anthropic"), base_url="http://x", api_key="k"
    )
    assert m.default_request_timeout == get_settings().litellm_timeout_s
    assert m.max_retries == 3
