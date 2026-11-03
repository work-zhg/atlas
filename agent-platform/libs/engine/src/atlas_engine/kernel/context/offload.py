"""大结果外置，全领域只有这一处（c4-model/agent/native-runtime/code-native-runtime-context.html）。

三个入口用同一套规则（预览格式、写到哪里、写不进去怎么办）：

    on_result  每次工具调用后：单条结果超过阈值
    on_batch   模型调用前：最后一批结果总量超过上限，从最大的挑
    on_input   模型调用前：用户一次贴进来的超长输入

有存储（模型能用 read_file 读回来）时，全文写进存储，消息里只留头尾预览和路径；没有存储时
只能截断，说明全文没有保存。文件读写工具不外置：read_file 自带分页，写类工具的结果本来就短。

★ 以前有两套：文件中间件逐条外置，压缩中间件按批外置，阈值、预览格式、写到哪里各管各的。
  文件中间件也因此成了「恒装的脚手架」—— 没勾文件工具也得装着它。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from atlas_engine.contracts import FilesystemProtocol
from atlas_engine.kernel.context._text import CHARS_PER_TOKEN, estimate_text, plain_text, text_of
from atlas_engine.kernel.middleware._fs_utils import (
    format_content_with_line_numbers,
    sanitize_tool_call_id,
)

logger = logging.getLogger(__name__)

__all__ = ["NOT_OFFLOADED", "OFFLOAD_DIR", "ResultOffloader", "batch_overflow"]

#: 外置的工具结果写在这里。★ 压缩后的工作集恢复跳过这个目录（它不是模型自己的文件）
OFFLOAD_DIR = "/large_tool_results"
#: 外置的超长用户输入写在这里
INPUT_DIR = "/conversation_history"
#: 不外置的工具：read_file 自带分页（外置了模型只会再用 read_file 读一遍），写类工具的结果本来就短
NOT_OFFLOADED = frozenset({"read_file", "write_file", "edit_file", "delete"})
#: 打过外置标记的用户输入（additional_kwargs 的键，值是全文的路径）
_OFFLOADED_TO = "offloaded_to"
#: 没有存储、整批封顶时，被挑中的结果截到多长
_BATCH_CLIP_CHARS = 2_000


def batch_overflow(messages: Sequence[BaseMessage], cap_tokens: int) -> list[ToolMessage]:
    """最后一批工具结果总量超过上限时，从最大的开始挑出要外置的。"""
    batch: list[ToolMessage] = []
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            break
        batch.append(m)
    sizes = [(estimate_text(text_of(m)), m) for m in batch]
    total = sum(s for s, _ in sizes)
    picked: list[ToolMessage] = []
    for size, m in sorted(sizes, key=lambda x: -x[0]):
        if total <= cap_tokens:
            break
        picked.append(m)
        total -= size
    return picked


def _preview(text: str, *, head_lines: int = 5, tail_lines: int = 5) -> str:
    """头尾各几行（带行号），中间标出省略了多少行。每行最多 1000 字符。"""
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines:
        return format_content_with_line_numbers([line[:1000] for line in lines], start_line=1)
    head = format_content_with_line_numbers([line[:1000] for line in lines[:head_lines]], start_line=1)
    tail = format_content_with_line_numbers(
        [line[:1000] for line in lines[-tail_lines:]], start_line=len(lines) - tail_lines + 1
    )
    return f"{head}\n... [{len(lines) - head_lines - tail_lines} lines truncated] ...\n{tail}"


def _clip(text: str, limit: int) -> str:
    """没有存储时只能截断：留头 70%、尾 30%。"""
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.7), int(limit * 0.3)
    return f"{text[:head]}\n\n…[省略中间 {len(text) - head - tail} 字符]…\n\n{text[-tail:]}"


def _with_text(message: BaseMessage, text: str) -> str | list[Any]:
    """换掉消息里的文本，图片等多模态块原样保留。"""
    if isinstance(message.content, str):
        return text
    media = [b for b in message.content_blocks if b["type"] != "text"]
    return [{"type": "text", "text": text}, *media] if media else text


class ResultOffloader:
    """大结果外置。store 是模型能用 read_file 读回来的那个文件系统；None = 只能截断。

    阈值都按 token 粗估（与预算同一个口径）。
    """

    def __init__(
        self,
        *,
        store: FilesystemProtocol | None,
        single_limit_tokens: int = 32_000,
        batch_cap_tokens: int | None = None,
        input_limit_tokens: int = 80_000,
    ) -> None:
        self._store = store
        self._single_limit = single_limit_tokens
        self._batch_cap = batch_cap_tokens
        self._input_limit = input_limit_tokens

    # ------------------------------------------------------------------ 入口

    async def on_result(self, result: ToolMessage | Command, *, tool_name: str) -> ToolMessage | Command:
        """一次工具调用的结果。Command 里带的 ToolMessage 逐条处理，其余原样放过。"""
        if tool_name in NOT_OFFLOADED:
            return result
        if isinstance(result, ToolMessage):
            return await self._single(result)
        update = result.update if isinstance(result, Command) else None
        if not isinstance(update, dict):
            return result
        messages = update.get("messages") or []
        wrapped = bool(messages) and isinstance(messages[0], RemoveMessage) and messages[0].id == REMOVE_ALL_MESSAGES
        body = messages[1:] if wrapped else messages
        processed = [await self._single(m) if isinstance(m, ToolMessage) else m for m in body]
        new_messages = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *processed] if wrapped else processed
        return Command(goto=result.goto, graph=result.graph, update={**update, "messages": new_messages})

    async def on_batch(self, messages: Sequence[BaseMessage]) -> list[ToolMessage]:
        """最后一批结果总量超过上限时，挑出来外置 → 替换后的消息（同 id）。"""
        if not self._batch_cap:
            return []
        out = []
        for m in batch_overflow(messages, self._batch_cap):
            if not m.id:
                continue  # 按 id 替换回 state，没有 id 的换不回去
            text = plain_text(m)
            path = await self._write(f"{OFFLOAD_DIR}/{sanitize_tool_call_id(str(m.tool_call_id))}", text)
            out.append(self._replace(m, text, path=path, subject="本批工具结果总量过大，此条",
                                     clip=_BATCH_CLIP_CHARS))  # fmt: skip
        return out

    async def on_input(self, messages: Sequence[BaseMessage]) -> HumanMessage | None:
        """最后一条是超长的用户输入 → 外置后的消息（同 id，打上标记，不会再外置一次）。"""
        last = messages[-1] if messages else None
        if not isinstance(last, HumanMessage) or not last.id or last.additional_kwargs.get(_OFFLOADED_TO):
            return None
        text = plain_text(last)
        if estimate_text(text) <= self._input_limit:
            return None
        path = await self._write(f"{INPUT_DIR}/{uuid.uuid4()}.md", text)
        if path is None:
            return None  # 用户的原话：写不进去就原样给模型，不截断
        note = (f"[消息过长，全文已保存到 {path}，需要时用 read_file 分页读取（offset / limit）]\n"
                f"预览（开头和结尾）：\n{_preview(text)}")  # fmt: skip
        return last.model_copy(update={
            "content": _with_text(last, note),
            "additional_kwargs": {**last.additional_kwargs, _OFFLOADED_TO: path},
        })  # fmt: skip

    # ------------------------------------------------------------------ 实现

    async def _single(self, message: ToolMessage) -> ToolMessage:
        text = plain_text(message)
        if estimate_text(text) <= self._single_limit:
            return message
        path = await self._write(f"{OFFLOAD_DIR}/{sanitize_tool_call_id(str(message.tool_call_id))}", text)
        return self._replace(message, text, path=path, subject="工具结果过大",
                             clip=int(self._single_limit * CHARS_PER_TOKEN))  # fmt: skip

    async def _write(self, path: str, text: str) -> str | None:
        """写进存储 → 路径；没有存储或写失败 → None（调用方改为截断）。"""
        if self._store is None:
            return None
        try:
            result = await self._store.awrite(path, text)
        except Exception:
            logger.warning("外置 %s 失败，改为截断", path, exc_info=True)
            return None
        if result is None or result.error:
            logger.warning("外置 %s 失败，改为截断：%s", path, getattr(result, "error", None))
            return None
        return path

    @staticmethod
    def _replace(message: ToolMessage, text: str, *, path: str | None, subject: str, clip: int) -> ToolMessage:
        if path is not None:
            body = (f"[{subject}，已外置] 完整内容已保存到 {path}，需要时用 read_file 分页读取（offset / limit）。\n"
                    f"预览（开头和结尾，中间省略的行标为 ... [N lines truncated] ...）：\n{_preview(text)}")  # fmt: skip
        else:
            body = f"[{subject}，已截断] 没能保存完整内容。\n{_clip(text, clip)}"
        return ToolMessage(
            content=cast("str | list[str | dict]", _with_text(message, body)),
            tool_call_id=message.tool_call_id,
            name=message.name,
            id=message.id,
            artifact=message.artifact,
            status=message.status,
            additional_kwargs=dict(message.additional_kwargs),
            response_metadata=dict(message.response_metadata),
        )
