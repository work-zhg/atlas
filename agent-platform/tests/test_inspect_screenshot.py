"""看浏览器截图回答问题（kernel/middleware/screen.py · inspect_screenshot）。

回答      主模型拿到的是一段文字，没有图；看图模型只收到这一张截图和问题，知道截图尺寸
不串流    看图模型的输出不混进对话正文
失败      不是图片、读不到、看图模型报错或没回答：回工具错误，不打断这一轮
审批      只读：手动模式下也直接执行
装配      开了浏览器、有沙箱、有工作区、交了看图模型才装
"""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from atlas_engine.contracts import ExecuteResponse, ReadResult
from atlas_engine.contracts.events import EventType
from atlas_engine.contracts.spec import AgentSpec, LimitSpec, ModelSpec
from atlas_engine.kernel.middleware.screen import (
    INSPECT_SCREENSHOT,
    MODEL_FAILED,
    NO_ANSWER,
    NOT_IMAGE,
    READ_FAILED,
    VISION_PROMPT,
)
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from tests.graphs import run_agent as run

from .fakes import TurnModel, tool_call_chunk

RUN_ID = UUID("77777777-7777-7777-7777-777777777777")
SHOT = "/data/files/2026-11-18/t1/.atlas/t1/browser/look-20261118-023443-1458.png"
QUESTION = "目标是不装 APP 下载 MP3：弹窗里该点哪一项？给出位置"
ANSWER = (
    "页面：《晴天》详情页，「请选择音质」弹窗遮住了页面\n"
    "回答：不用装 APP 的是第 2 项\n"
    "位置：「#2 下载低品质MP3」(634, 401)"
)


def _png(width: int = 1280, height: int = 720) -> str:
    """只有文件头的 PNG（够看出尺寸），base64。"""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00" * 64
    return base64.b64encode(raw).decode()


class Workspace:
    """工作区的替身：看截图只用 aread。"""

    def __init__(self, files: dict[str, ReadResult]) -> None:
        self.files = files

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.files.get(path) or ReadResult(error=f"文件不存在：{path}")


def _workspace() -> Workspace:
    return Workspace(
        {
            SHOT: ReadResult(file_data={"content": _png(), "encoding": "base64"}),
            "/data/files/notes.txt": ReadResult(file_data={"content": "x", "encoding": "utf-8"}),
        }
    )


class Eyes(BaseChatModel):
    """看图模型的替身：记下收到的消息，回一段文字或抛错。

    ★ 要能流式：真实模型在图里是流式调用的，不打 nostream 时它的 token 才会混进 messages 流。
      只有 _generate 的替身不走流式，测不出这个泄漏。
    """

    reply: str = ANSWER
    error: Exception | None = None
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "eyes-fake"

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._seen.append(list(messages))
        if self.error is not None:
            raise self.error
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self._seen.append(list(messages))
        if self.error is not None:
            raise self.error
        # 空回答也吐一个空块：真实模型至少有 message_start，一个块都没有框架会直接报错
        for line in self.reply.splitlines(keepends=True) or [""]:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=line))
            if run_manager is not None:
                await run_manager.on_llm_new_token(line, chunk=chunk)
            yield chunk


class Shell:
    id = "sbx:test"

    def execute(self, command: str, **kw: Any) -> ExecuteResponse:
        raise NotImplementedError

    async def aexecute(self, command: str, **kw: Any) -> ExecuteResponse:
        return ExecuteResponse(output="", exit_code=0)


def _spec(*, browser: bool = True) -> AgentSpec:
    return AgentSpec(
        slug="looker",
        name="看图助理",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("bash",),
        limits=LimitSpec(timeout_s=20),
        browser=browser,
    )


def _model(path: str = SHOT, question: str = QUESTION) -> TurnModel:
    args = json.dumps({"path": path, "question": question}, ensure_ascii=False)
    return TurnModel(
        scripts=[[tool_call_chunk(INSPECT_SCREENSHOT, args, "c1")], [AIMessageChunk(content="好")]]
    )


async def _inspect(
    *,
    eyes: Any = None,
    model: TurnModel | None = None,
    spec: AgentSpec | None = None,
    sandbox: Any = None,
    workspace: Any = None,
    approval_mode: str | None = None,
) -> tuple[list, TurnModel]:
    model = model or _model()
    events = [
        e
        async for e in run(
            spec or _spec(),
            run_id=RUN_ID,
            model=model,
            input_content="帮我下载周杰伦的《晴天》",
            sandbox=sandbox or Shell(),
            filesystem=workspace or _workspace(),
            vision_model=eyes,
            approval_mode=approval_mode,
        )
    ]
    return events, model


def _tool_result(model: TurnModel) -> ToolMessage:
    seen = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert len(seen) == 1, seen
    return seen[0]


# ---------------------------------------------------------------- 回答


async def test_answer_goes_back_as_text_without_the_image() -> None:
    eyes = Eyes()
    events, model = await _inspect(eyes=eyes)
    result = _tool_result(model)
    assert result.content == ANSWER
    assert result.status == "success"
    completed = next(e for e in events if e.type is EventType.TOOL_COMPLETED)
    assert (completed.data["name"], completed.data["result"]) == (INSPECT_SCREENSHOT, ANSWER)
    assert events[-1].type is EventType.RUN_FINISHED


async def test_vision_model_sees_only_this_screenshot_and_the_question() -> None:
    eyes = Eyes()
    await _inspect(eyes=eyes)
    [request] = eyes.seen
    system, human = request
    assert isinstance(system, SystemMessage) and isinstance(human, HumanMessage)
    assert system.content == VISION_PROMPT.format(size="宽 1280、高 720 像素")
    image, text = human.content_blocks
    assert (image["type"], image["base64"], image["mime_type"]) == ("image", _png(), "image/png")
    assert (text["type"], text["text"]) == ("text", f"问题：{QUESTION}")


async def test_vision_output_is_not_streamed_as_the_answer() -> None:
    events, _ = await _inspect(eyes=Eyes())
    deltas = "".join(e.data["text"] for e in events if e.type is EventType.MESSAGE_DELTA)
    assert deltas == "好"


# ---------------------------------------------------------------- 失败


async def test_not_an_image_goes_back_as_an_error() -> None:
    eyes = Eyes()
    _, model = await _inspect(eyes=eyes, model=_model(path="/data/files/notes.txt"))
    result = _tool_result(model)
    assert (result.content, result.status) == (
        NOT_IMAGE.format(path="/data/files/notes.txt"),
        "error",
    )
    assert eyes.seen == []


async def test_missing_screenshot_goes_back_as_an_error() -> None:
    eyes = Eyes()
    missing = "/data/files/look-missing.png"
    events, model = await _inspect(eyes=eyes, model=_model(path=missing))
    result = _tool_result(model)
    assert (result.content, result.status) == (
        READ_FAILED.format(error=f"文件不存在：{missing}"),
        "error",
    )
    assert eyes.seen == []
    assert [e.data["call_id"] for e in events if e.type is EventType.TOOL_FAILED] == ["c1"]
    assert events[-1].type is EventType.RUN_FINISHED


async def test_model_failure_goes_back_as_an_error() -> None:
    eyes = Eyes(error=RuntimeError("429 Token Plan 用完"))
    events, model = await _inspect(eyes=eyes)
    result = _tool_result(model)
    assert (result.content, result.status) == (
        MODEL_FAILED.format(error="RuntimeError: 429 Token Plan 用完"),
        "error",
    )
    assert events[-1].type is EventType.RUN_FINISHED


async def test_empty_answer_goes_back_as_an_error() -> None:
    _, model = await _inspect(eyes=Eyes(reply=""))
    result = _tool_result(model)
    assert (result.content, result.status) == (NO_ANSWER, "error")


# ---------------------------------------------------------------- 审批与装配


async def test_read_only_so_manual_mode_does_not_ask() -> None:
    eyes = Eyes()
    events, model = await _inspect(eyes=eyes, approval_mode="manual")
    assert EventType.APPROVAL_REQUIRED not in [e.type for e in events]
    assert _tool_result(model).content == ANSWER


async def test_tool_only_with_browser_sandbox_workspace_and_vision_model() -> None:
    """缺一样就不装：模型调了也是未知工具，看图模型不会被调用。"""
    cases: list[dict[str, Any]] = [
        {"spec": _spec(browser=False)},
        {"sandbox": False},
        {"workspace": False},
        {"no_eyes": True},
    ]
    for case in cases:
        eyes = Eyes()
        model = _model()
        events = [
            e
            async for e in run(
                case.get("spec", _spec()),
                run_id=RUN_ID,
                model=model,
                input_content="x",
                sandbox=None if case.get("sandbox") is False else Shell(),
                filesystem=None if case.get("workspace") is False else _workspace(),
                vision_model=None if case.get("no_eyes") else eyes,
            )
        ]
        assert eyes.seen == [], case
        assert _tool_result(model).status == "error", case
        assert events[-1].type is EventType.RUN_FINISHED, case
