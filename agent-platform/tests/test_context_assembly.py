"""持久指令与记忆附件的装配（doc/context-management-design.html §10 / §11）。"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import pytest
from atlas_agent.config import Settings
from atlas_agent.executor import context as ctx

_BASE = {"database_url": "postgresql+asyncpg://x:y@localhost/z", "litellm_key": "k"}


class _FS:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> Any:
        if path not in self.files:
            raise FileNotFoundError(path)

        class R:
            error = None

            def __init__(self, c: str) -> None:
                self.file_data = {"content": c}

        return R(self.files[path])


class _Config:
    payload: ClassVar[dict[str, str]] = {}
    fail = False

    def __init__(self, *_a: Any, **_k: Any) -> None: ...

    async def get(self, path: str, **params: Any) -> Any:
        if _Config.fail:
            raise RuntimeError("config down")
        assert path == "/internal/instructions" and params["user_id"]
        return _Config.payload


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctx, "ConfigClient", _Config)
    _Config.payload = {"platform": "", "user": ""}
    _Config.fail = False


def _settings(**kw: Any) -> Settings:
    return Settings(**_BASE, config_base_url="http://cfg", **kw)


async def test_layers_are_ordered_platform_user_workspace() -> None:
    _Config.payload = {"platform": "遵守合规", "user": "默认中文"}
    fs = _FS({"/workspace/AGENTS.md": "用 pnpm"})
    text = await ctx.load_instructions(_settings(), fs, uuid4())
    assert text.index("遵守合规") < text.index("默认中文") < text.index("用 pnpm")
    assert "工作区约定（/workspace/AGENTS.md）" in text


async def test_atlas_md_preferred_and_empty_means_nothing() -> None:
    fs = _FS({"/workspace/ATLAS.md": "A", "/workspace/AGENTS.md": "B"})
    text = await ctx.load_instructions(_settings(), fs, uuid4())
    assert "ATLAS.md" in text and "\nB" not in text
    assert await ctx.load_instructions(_settings(), _FS({}), uuid4()) == ""


async def test_config_outage_does_not_fail_the_turn() -> None:
    _Config.fail = True
    text = await ctx.load_instructions(
        _settings(), _FS({"/workspace/ATLAS.md": "仍然有我"}), uuid4()
    )
    assert "仍然有我" in text


async def test_long_layers_are_clipped() -> None:
    _Config.payload = {"platform": "x" * 50_000, "user": ""}
    text = await ctx.load_instructions(_settings(), None, uuid4())
    assert len(text) < 11_000 and "已截断" in text


class _Mem:
    def __init__(self, hits: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.hits, self.fail, self.queries = hits or [], fail, []

    async def search(self, q: str, **kw: Any) -> list[dict[str, Any]]:
        self.queries.append(q)
        if self.fail:
            raise RuntimeError("mem down")
        return self.hits


async def test_memory_attachment_from_current_question() -> None:
    mem = _Mem([{"id": "abcdef123456", "memory": "用户偏好用 bun"}])
    out = await ctx.memory_attachments(
        _settings(), mem, user_id=uuid4(),
        input_content=[{"type": "text", "text": "帮我装依赖"}], thread_memory="on",
    )  # fmt: skip
    assert out[0][0] == "memory" and "用户偏好用 bun" in out[0][1] and "可能已过期" in out[0][1]
    assert mem.queries == ["帮我装依赖"]


@pytest.mark.parametrize(
    ("mem", "content", "thread_memory", "settings_kw"),
    [
        (None, "问题", "on", {}),
        (_Mem([{"memory": "x"}]), "问题", "off", {}),
        (_Mem([{"memory": "x"}]), "", "on", {}),
        (_Mem([{"memory": "x"}]), "问题", "on", {"memory_auto_inject": False}),
        (_Mem(fail=True), "问题", "on", {}),
    ],
)
async def test_memory_attachment_skips(
    mem: Any, content: str, thread_memory: str, settings_kw: Any
) -> None:
    out = await ctx.memory_attachments(
        _settings(**settings_kw), mem, user_id=uuid4(), input_content=content,
        thread_memory=thread_memory,
    )  # fmt: skip
    assert out == []


def test_extract_summary_falls_back_to_unclosed_analysis() -> None:
    from atlas_agent.services.compaction import _extract_summary

    assert _extract_summary("<analysis>草稿</analysis>\n<summary>正文</summary>") == "正文"
    assert _extract_summary("<analysis>草稿</analysis>\n直接写的摘要") == "直接写的摘要"
    # 模型只写了草稿就结束：退回草稿，而不是空串
    assert _extract_summary("<analysis>\n1. 用户要写 wordfreq") == "1. 用户要写 wordfreq"
    assert _extract_summary("<analysis>草稿</analysis>\n<summary>") == "草稿"


def test_summarizer_model_treats_legacy_default_as_unset() -> None:
    from atlas_agent.domain.spec import resolve_summarizer_model

    assert resolve_summarizer_model("", "deepseek-chat") == "deepseek-chat"
    assert resolve_summarizer_model(None, "deepseek-chat") == "deepseek-chat"
    assert resolve_summarizer_model("claude-haiku-4-5", "deepseek-chat") == "deepseek-chat"
    assert resolve_summarizer_model("qwen-max", "deepseek-chat") == "qwen-max"
