"""测试用的假模型。

runner 内部把模型交给 kernel 建图，图会调用 `bind_tools`
等真实 BaseChatModel 接口 —— 只实现 `astream` 的鸭子类型不再够用。

不用 langchain 自带的 GenericFakeChatModel：它 bind_tools 未实现，
且无法精确控制 usage_metadata 与 content blocks。自己写一个脚本化的，
把"吐哪些 chunk"完全攥在手里，整条链路（含图与中间件）即可脱离网络运行。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field, PrivateAttr


class ScriptedChatModel(BaseChatModel):
    """按脚本吐 chunk 的假模型。

    chunks: 逐个产出的 AIMessageChunk（可带 content blocks / usage_metadata）
    delay:  每个 chunk 前的等待，用于超时与取消
    error:  非 None 时直接抛出，用于故障路径
    """

    chunks: list[AIMessageChunk] = Field(default_factory=list)
    delay: float = 0.0
    error: Any = None

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        # 绑定对假模型无意义，但 langchain 的 agent 工厂必然会调它
        return self

    def _iter_chunks(self) -> Iterator[AIMessageChunk]:
        yield from self.chunks

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if self.error is not None:
            raise self.error
        for chunk in self._iter_chunks():
            if self.delay:
                await asyncio.sleep(self.delay)
            yield ChatGenerationChunk(message=chunk)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.error is not None:
            raise self.error
        text = "".join(c.content if isinstance(c.content, str) else "" for c in self.chunks)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def text_model(text: str = "你好，世界", *, pieces: int = 1) -> ScriptedChatModel:
    """把 text 切成 pieces 段流式吐出。"""
    if pieces <= 1:
        parts = [text]
    else:
        size = max(1, len(text) // pieces)
        parts = [text[i : i + size] for i in range(0, len(text), size)]
    return ScriptedChatModel(chunks=[AIMessageChunk(content=p) for p in parts])


def usage_model(text: str, usage: dict[str, Any]) -> ScriptedChatModel:
    return ScriptedChatModel(
        chunks=[
            AIMessageChunk(content=text),
            AIMessageChunk(content="", usage_metadata=usage),
        ]
    )


def blocks_model(blocks_per_chunk: Sequence[list[dict]]) -> ScriptedChatModel:
    """按 content blocks 形态吐 —— 用于 thinking 与 text 混排。"""
    return ScriptedChatModel(
        chunks=[AIMessageChunk(content=b) for b in blocks_per_chunk]  # type: ignore[arg-type]
    )


class TurnModel(BaseChatModel):
    """每次被调用吐不同的脚本。

    ScriptedChatModel 每次都吐同一串，测不了多轮 —— 而工具调用天然是多轮：
    第一轮模型发 tool_call，工具执行后第二轮才给结论。子智能体更是必须
    多轮（主 agent 发 task，拿到结果后再总结）。

    脚本用尽后重复最后一段，避免图多转一圈就 IndexError。
    """

    scripts: list[list[AIMessageChunk]] = Field(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "turn-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    @property
    def call_count(self) -> int:
        return self._calls

    def _script(self) -> list[AIMessageChunk]:
        index = min(self._calls, len(self.scripts) - 1)
        self._calls += 1
        return self.scripts[index] if self.scripts else []

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._script():
            yield ChatGenerationChunk(message=chunk)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = "".join(c.content if isinstance(c.content, str) else "" for c in self._script())
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


class ReplyModel(BaseChatModel):
    """按顺序回复完整 AIMessage 的假模型 —— 给 ainvoke 型调用方用。

    TurnModel/_generate 只拼接文本会弄丢 usage_metadata，而 research loop
    这类复合工具恰恰要靠它测 usage.delta 入账。脚本用尽后重复最后一条。
    """

    replies: list[AIMessage] = Field(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "reply-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    @property
    def call_count(self) -> int:
        return self._calls

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        index = min(self._calls, len(self.replies) - 1)
        self._calls += 1
        message = self.replies[index] if self.replies else AIMessage(content="")
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_call_chunk(name: str, args_json: str, call_id: str) -> AIMessageChunk:
    """构造一个发起工具调用的 chunk。"""
    return AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": name, "args": args_json, "id": call_id, "index": 0}],
    )


def raising_model(exc: Exception) -> ScriptedChatModel:
    return ScriptedChatModel(chunks=[], error=exc)


def slow_model(text: str, delay: float = 0.3, *, pieces: int = 5) -> ScriptedChatModel:
    model = text_model(text, pieces=pieces)
    model.delay = delay
    return model
