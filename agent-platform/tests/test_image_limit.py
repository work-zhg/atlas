"""主上下文里的图片上限（kernel/context/images.py · 上下文流水线 ①½）。

只留最近几张   工具结果里的图只留最近 keep 张，更早的换成占位（同 id、带路径）；图旁边的文字留着
最后一批      最近一条 AIMessage 之后的结果从不动，即使超过 keep 张
用户的图      HumanMessage 里的图不动
流水线        每次调模型前写回 state；线上的流水线开没开压缩都装着
"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_engine.contracts.spec import AgentSpec, ModelSpec
from atlas_engine.kernel.context import ContextMiddleware, ImageLimit
from atlas_engine.kernel.context.images import IMAGE_PLACEHOLDER, IMAGE_PLACEHOLDER_NO_PATH
from atlas_engine.runtime.context import ContextLimits, ContextPorts, context_pipeline
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

PNG = {"type": "image", "base64": "iVBORw0KGgo=", "mime_type": "image/png"}


def _call(n: int) -> AIMessage:
    return AIMessage(
        content="", id=f"a{n}", tool_calls=[{"id": f"c{n}", "name": "read_file", "args": {}}]
    )


def _shot(n: int, *, path: bool = True, text: str | None = None) -> ToolMessage:
    blocks: list[Any] = [{"type": "text", "text": text}] if text else []
    blocks.append(PNG)
    return ToolMessage(
        content_blocks=blocks,
        tool_call_id=f"c{n}",
        name="read_file",
        id=f"t{n}",
        additional_kwargs={"read_file_path": f"/data/look-{n}.png"} if path else {},
    )


def _conversation(shots: int) -> list[BaseMessage]:
    """用户提问，然后每轮：模型调 read_file → 一张图。最后一张是最后一批（还没给模型看）。"""
    out: list[BaseMessage] = [HumanMessage(content="帮我下载晴天", id="h0")]
    for n in range(1, shots + 1):
        out += [_call(n), _shot(n)]
    return out


def _types(message: BaseMessage) -> list[str]:
    return [b["type"] for b in message.content_blocks]


def test_only_the_most_recent_images_stay() -> None:
    plan = ImageLimit(keep=2).plan(_conversation(4))
    assert [m.id for m in plan] == ["t1", "t2"]
    first = plan[0]
    assert (first.tool_call_id, first.name, first.status) == ("c1", "read_file", "success")
    assert first.content_blocks == [
        {"type": "text", "text": IMAGE_PLACEHOLDER.format(keep=2, path="/data/look-1.png")}
    ]
    assert ImageLimit(keep=2).plan(_conversation(2)) == []


def test_text_next_to_the_image_stays_and_no_path_has_its_own_placeholder() -> None:
    messages = [_call(1), _shot(1, path=False, text="截图：look-1"), _call(2), _shot(2), _call(3)]
    [replaced] = ImageLimit(keep=1).plan(messages)
    assert replaced.content_blocks == [
        {"type": "text", "text": "截图：look-1"},
        {"type": "text", "text": IMAGE_PLACEHOLDER_NO_PATH.format(keep=1)},
    ]


def test_the_last_batch_is_never_touched() -> None:
    """模型一批读了 3 张、还没看到：即使超过 keep 也都留着；之前的照常换掉。"""
    messages = [
        *_conversation(1),
        AIMessage(
            content="",
            id="a9",
            tool_calls=[{"id": f"c{n}", "name": "read_file", "args": {}} for n in (2, 3, 4)],
        ),
        _shot(2),
        _shot(3),
        _shot(4),
    ]
    assert [m.id for m in ImageLimit(keep=2).plan(messages)] == ["t1"]


def test_user_images_are_not_touched() -> None:
    messages = [
        HumanMessage(content_blocks=[PNG, {"type": "text", "text": "看看这张图"}], id="h0"),
        _call(1),
        _shot(1),
        _call(2),
    ]
    assert ImageLimit(keep=1).plan(messages) == []


def test_keep_at_least_one() -> None:
    with pytest.raises(ValueError, match="至少留 1 张"):
        ImageLimit(keep=0)


async def test_middleware_writes_the_replacements_back_to_state() -> None:
    update = await ContextMiddleware(images=ImageLimit(keep=1)).abefore_model(
        {"messages": [*_conversation(3), _call(4)]}
    )
    assert update is not None
    assert [(m.id, _types(m)) for m in update["messages"]] == [("t1", ["text"]), ("t2", ["text"])]


def _spec() -> AgentSpec:
    return AgentSpec(slug="a", name="a", system_prompt="", model=ModelSpec(model="m"))


@pytest.mark.parametrize("compaction", [False, True], ids=["没开压缩", "开了压缩"])
async def test_the_pipeline_always_limits_images(compaction: bool) -> None:
    limits = (
        ContextLimits(window=200_000, model_max_output=8_192, summarizer=None)
        if compaction
        else None
    )
    pipeline = context_pipeline(
        _spec(),
        ContextPorts(
            offload_store=None, result_limit_tokens=100_000, batch_cap_tokens=200_000, limits=limits
        ),
    )
    update = await pipeline.abefore_model({"messages": [*_conversation(4), _call(5)]})
    assert update is not None
    assert [m.id for m in update["messages"]] == ["t1", "t2"]
