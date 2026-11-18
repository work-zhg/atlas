"""主上下文里的图片上限：只留最近几张，更早的换成一句占位（上下文流水线 ①½）。

截图一张约 1.3k token，按 token 算永远到不了清理档；但 MiniMax 这类模型处理带图请求随图片数变慢
（实测 0 张 1–3 秒、2 张中位约 8 秒、11 张中位约 24 秒），computer use 做十几步就越来越慢。看图本该用
inspect_screenshot（图不进主上下文），但模型偶尔改用 read_file 自己看、此后一直这样（2026-11-18 两个
会话，图攒到 6–7 张时首字 30–106 秒）—— 这里兜底：不管图是怎么进来的，主上下文里最多留最近 keep 张。

★ 只动工具结果里的图片（read_file 读的截图、MCP 返回的图）；用户自己发的图不动。
★ 最后一批（最近一条 AIMessage 之后的）结果从不动：模型马上就要看它们。
★ 同 id 替换、写回 state：换掉的不会再变，提示词缓存的前缀只在又有图被换掉时断一次。
★ 不发事件：computer use 几乎每一步都会换掉一张，界面上不该每步一条「已清除」。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

__all__ = ["IMAGE_PLACEHOLDER", "IMAGE_PLACEHOLDER_NO_PATH", "ImageLimit"]

IMAGE_PLACEHOLDER = (
    "[较早的图片已移出上下文（只留最近 {keep} 张）：{path}。要再看，用 inspect_screenshot 问，"
    "或 read_file 重新读]"
)
IMAGE_PLACEHOLDER_NO_PATH = "[较早的图片已移出上下文（只留最近 {keep} 张）]"


def _has_image(message: BaseMessage) -> bool:
    return any(block.get("type") == "image" for block in message.content_blocks)


class ImageLimit:
    """主上下文里最多留最近 keep 张工具结果里的图。"""

    def __init__(self, *, keep: int = 2) -> None:
        if keep < 1:
            raise ValueError("至少留 1 张：模型刚读的图要能看到")
        self._keep = keep

    def plan(self, messages: Sequence[BaseMessage]) -> list[ToolMessage]:
        """要换掉的旧图 → 替换后的 ToolMessage（同 id，按 id 去重的 reducer 会原地替换）。"""
        last_ai = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=-1)
        holders = [
            (i, m)
            for i, m in enumerate(messages)
            if isinstance(m, ToolMessage) and m.id and _has_image(m)
        ]
        stale = holders[: max(0, len(holders) - self._keep)]
        return [self._without_images(m) for i, m in stale if i < last_ai]

    def _without_images(self, message: ToolMessage) -> ToolMessage:
        path = message.additional_kwargs.get("read_file_path")
        note = (
            IMAGE_PLACEHOLDER.format(keep=self._keep, path=path)
            if path
            else IMAGE_PLACEHOLDER_NO_PATH.format(keep=self._keep)
        )
        blocks: list[Any] = [
            {"type": "text", "text": note} if block.get("type") == "image" else block
            for block in message.content_blocks
        ]
        return ToolMessage(
            content=blocks,
            tool_call_id=message.tool_call_id,
            name=message.name,
            id=message.id,
            status=message.status,
            additional_kwargs=message.additional_kwargs,
        )
