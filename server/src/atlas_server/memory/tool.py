"""只读的记忆检索工具（记忆设计 §06 辅路径）。

上线顺序的第二步（§12）：先只抽取与存储、观察质量，**再开检索工具**，
最后才开自动注入。工具调用是模型的主动选择且结果可见，出错时影响面
远小于无声注入 —— 这是它排在注入之前的理由。

★ 只有 search，没有 add / update / delete。

  · 不给写工具（§05）：顾问支持 regenerate，同一轮可能被重跑多次，
    每次都会再写一遍记忆 —— 而它在 UI 上仍然是个看起来安全的重试按钮。
    写记忆改变的是**跨会话的持久状态**，重跑一次就多一条。
  · 不给管理工具（§10）：删除/更正是用户的权利，不是 Agent 的能力。
    挂给模型意味着它在处理不可信文本（代码仓库、网页、工具输出）时
    可能被诱导删除用户数据。

  纯读取则相反 —— 重跑多少次都不改变任何状态，可以安全地挂进工具集。

★ user_id **不在参数 schema 里**（§09 的隔离基础）。

  Mem0 的 filters 决定了能读到谁的记忆。一旦 user_id 成为工具参数，
  模型就能（被诱导）指定任意 user_id 读取他人记忆 —— 而提示词注入是
  真实存在的攻击面。这里靠闭包把它钉死在会话所有者上：工具签名里
  根本没有那个参数，模型无从指定。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .client import MemoryClient

logger = logging.getLogger(__name__)

__all__ = ["MEMORY_SEARCH_TOOL", "MemoryUnavailable", "make_memory_search_tool"]


class MemoryUnavailable(RuntimeError):
    """勾了 search_memory 却没开记忆 —— 明确报错（§13.2）。

    与 web_search 缺 SERPAPI_KEY 同款取舍：宁可这一轮失败并说清原因，
    也不要静默少装一个工具 —— 那样模型以为自己没有记忆可查，
    而用户以为记忆在工作，两边都不知道哪里不对。
    """

MEMORY_SEARCH_TOOL = "search_memory"

#: 检索上限与相关度门槛。往回拉太多低相关内容只会挤占上下文（§11）。
_TOP_K = 5
_THRESHOLD = 0.3


class _Args(BaseModel):
    query: str = Field(
        description=(
            "要回忆的内容，用自然语言描述。"
            "例如「用户的包管理器偏好」「这个项目的测试命令」。"
        )
    )


def format_hits(query: str, hits: list[dict]) -> str:
    """给模型看的纯文本。

    ★ 空结果要**明说**，不要返回空串 —— 否则模型会把「没查到」误解成
      工具坏了，或者干脆无视这次调用的结果继续凭空作答。
    """
    if not hits:
        return f"没有找到与「{query}」相关的记忆。这可能是第一次遇到这个话题。"
    lines = [f"与「{query}」相关的记忆（{len(hits)} 条）："]
    lines.extend(f"{i}. {h.get('memory', '')}" for i, h in enumerate(hits, 1))
    return "\n".join(lines)


def make_memory_search_tool(memory: MemoryClient, *, user_id: UUID) -> StructuredTool:
    """造一个绑定到某个用户的检索工具。

    user_id 走闭包捕获 —— 见模块开头：它不能出现在模型可填的参数里。
    """

    async def search_memory(query: str) -> str:
        try:
            hits = await memory.search(
                query, user_id=user_id, top_k=_TOP_K, threshold=_THRESHOLD
            )
        except Exception:
            # ★ 记忆不可用时返回**明确的错误信息**，让模型知道它暂时用不了
            #   这个工具（§11）。抛出去会让整轮 run 失败 —— 而记忆只是辅助，
            #   没有它照样能回答。
            logger.warning("记忆检索失败", exc_info=True)
            return "记忆服务暂时不可用，这次回答不依赖历史记忆。"
        return format_hits(query, hits)

    return StructuredTool.from_function(
        coroutine=search_memory,
        name=MEMORY_SEARCH_TOOL,
        description=(
            "检索关于该用户的跨会话记忆：偏好、习惯、项目约定等在之前的"
            "会话里提到过、且长期成立的事实。"
            "当用户提到「我之前说过」「按我的习惯」，或你需要确认他的偏好时使用。"
            "只读 —— 它不会写入或修改任何记忆。"
        ),
        args_schema=_Args,
    )
