"""前缀缓存的开关：编辑器的 prompt_cache 与模型目录的 supports_cache 共同决定打不打 cache_control。

2026-10-19 之前 kernel 对所有 ChatAnthropic 无条件装缓存中间件，这两个字段运行时没人读 ——
用户关掉开关缓存照开，DeepSeek（实测 cache_control 无效）照样被塞标记。

全部看**送到模型的请求**：只查中间件栈的话，装了但 TTL 没传进去这类错一样会通过。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from atlas_agent.config import Settings
from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk, SystemMessage
from langchain_core.outputs import ChatGenerationChunk

from tests.graphs import run_agent as run

_SEEN: list[dict[str, Any]] = []


class _RecordingAnthropic(ChatAnthropic):
    """不发网络请求：记下送进来的系统提示词与参数，回一句话。"""

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        system = next((m for m in messages if isinstance(m, SystemMessage)), None)
        _SEEN.append({"kwargs": kwargs, "system": system.content if system else None})
        yield ChatGenerationChunk(message=AIMessageChunk(content="好"))


def _spec(model: str = "claude-sonnet-5", *, prompt_cache: bool = True) -> AgentSpec:
    return AgentSpec(
        slug="cache-probe",
        name="缓存探针",
        system_prompt="你是客服助手。",
        model=ModelSpec(model=model, prompt_cache=prompt_cache),
        tool_names=(),
        limits=LimitSpec(max_steps=4),
    )


async def _request(**build_kw: Any) -> dict[str, Any]:
    _SEEN.clear()
    model = _RecordingAnthropic(model="claude-sonnet-5", api_key="test")
    async for _ in run(_spec(), model, run_id=uuid4(), input_content="你好", **build_kw):
        pass
    assert len(_SEEN) == 1
    return _SEEN[0]


async def test_cache_control_reaches_the_model_with_the_given_ttl() -> None:
    seen = await _request(prompt_cache_ttl="1h")

    assert seen["kwargs"].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    # 系统提示词的最后一块也打了标记
    assert isinstance(seen["system"], list)
    assert seen["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


async def test_no_ttl_means_no_cache_control_at_all() -> None:
    seen = await _request()

    assert "cache_control" not in seen["kwargs"]
    assert seen["system"] == "你是客服助手。"


# ── assembly：spec 开关 × 模型目录 ────────────────────────────────────────────


async def _ttl(spec: AgentSpec, ttl: str = "1h") -> str | None:
    from atlas_agent.db.session import get_sessionmaker
    from atlas_agent.executor.assembly import HookAssembly

    host = SimpleNamespace(
        _sessionmaker=get_sessionmaker(), _settings=SimpleNamespace(prompt_cache_ttl=ttl)
    )
    return await HookAssembly._prompt_cache_ttl(host, SimpleNamespace(spec=spec))  # type: ignore[arg-type]


async def test_assembly_uses_the_configured_ttl_when_the_model_supports_cache() -> None:
    assert await _ttl(_spec("claude-sonnet-5")) == "1h"
    assert await _ttl(_spec("claude-sonnet-5"), ttl="5m") == "5m"


async def test_assembly_honours_the_editor_switch() -> None:
    assert await _ttl(_spec("claude-sonnet-5", prompt_cache=False)) is None


async def test_assembly_skips_models_measured_without_cache_support() -> None:
    """DeepSeek：cache_control 被接受但不起作用（迁移 0008），不塞。"""
    assert await _ttl(_spec("deepseek-v4-pro")) is None


async def test_assembly_treats_models_missing_from_the_catalog_as_unsupported() -> None:
    assert await _ttl(_spec("no-such-model")) is None


# ── 配置：TTL 只有一个来源 ─────────────────────────────────────────────────────


def test_compaction_idle_rule_follows_the_same_ttl() -> None:
    assert Settings(prompt_cache_ttl="5m").prompt_cache_ttl_s == 300.0
    assert Settings(prompt_cache_ttl="1h").prompt_cache_ttl_s == 3600.0


def test_only_the_two_anthropic_ttls_are_accepted() -> None:
    with pytest.raises(ValueError, match="prompt_cache_ttl"):
        Settings(prompt_cache_ttl="10m")  # type: ignore[arg-type]
