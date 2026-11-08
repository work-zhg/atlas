"""存储格式的消息：message 表里一行长什么样（与框架无关）。

存的是 **Anthropic 原生 content blocks**，不是 LangChain 的序列化形式：这张表要留几年，
不能绑在框架的版本上。role 只有 user / assistant，区分对话轮和工具结果靠 kind：tool_result
本来就是 user 角色的 block，tool_use 是 assistant 的 block。

和 LangChain 消息之间的互转在 atlas_engine.runtime.messages —— 只有原生运行时需要它；
这里的东西 server（落库、接管时收拾现场）和运行时都用。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "INTERRUPTED_RESULT",
    "KIND_CHAT",
    "KIND_TOOL_RESULT",
    "StoredLike",
    "StoredMessage",
    "answered_calls",
    "dangling_calls",
    "interrupted_block",
    "unknown_results",
]

#: 对话轮：用户的提问、助手的回答（后者可携带 tool_use blocks）。
KIND_CHAT = "chat"

#: 工具结果：role=user，content 是 tool_result blocks。
#:
#: ★ 必须与 chat 分开。它也是 role=user，而「最后一条 user 消息是本轮输入」
#:   这条判据不加区分就会把一批工具结果当成用户的新提问 —— 表现是模型莫名
#:   回答一段 JSON，而用户的问题被当成历史。
KIND_TOOL_RESULT = "tool_result"


class StoredLike(Protocol):
    """存储行的形状。ORM 的 Message 与测试里的 dataclass 都满足它。"""

    @property
    def role(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def content(self) -> Any: ...


@dataclass(frozen=True)
class StoredMessage:
    """一条待写入（或刚读出）的消息。`to_storage` 的产物。"""

    role: str
    kind: str
    content: list[dict[str, Any]]


#: 补给中断的工具调用的结果文本。
#:
#: ★ 为什么要补，而不是把那个 tool_use 丢掉。一轮在工具执行途中失败（网关
#:   挂了、进程被杀、超时）时，序列里会留下一个没有结果的 tool_use。下一轮
#:   带着它重建历史，provider 直接回 400 —— 而报错指向「消息格式」，根因
#:   却在上一个 run 的收尾。
#:
#: ★ 丢掉 tool_use 也能满足格式，但那等于告诉模型「这次调用没发生过」。
#:   它会原样重试一遍，而中断的工具可能已经产生了副作用（文件已经写了一半）。
#:   如实说「中断，结果未知」才让它有机会先去确认状态。
INTERRUPTED_RESULT = "工具执行被中断，结果未知（上一轮未正常结束）。"

#: 接管时补给悬空调用的结果（横向扩展设计 §5.1）。与 INTERRUPTED_RESULT 的区别：
#: 那一轮并没有结束，是执行它的实例中途退出、由别的实例接着跑 —— 模型要据此先查状态。
_TAKEOVER_RESULT = (
    "执行这个工具的服务实例中途退出，工具是否已执行、执行到哪一步都未知。"
    "请先检查当前状态，再决定是否重试。"
)


def interrupted_block(call_id: str, text: str = INTERRUPTED_RESULT) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": text,
        "is_error": True,
        "atlas_name": "",
    }


def _is_type(block: Any, type_: str) -> bool:
    return isinstance(block, dict) and block.get("type") == type_


def dangling_calls(rows: Sequence[StoredLike]) -> list[tuple[str, str]]:
    """接管时：库里最后一条带 tool_use 的回答，有哪些调用没有结果 → [(tool_call_id, 工具名)]。

    ★ 那些调用的 tool_use 已在检查点里落库（意图记录），结果却没有 —— 进程死在工具执行中。
      怎么处理由续跑领域的 DanglingCallPolicy 决定：工具声明了可接续的用同一个调用重放，
      其余的补「结果未知」（unknown_results）。都不处理的话 SuspensionMiddleware 在图入口
      会补成「被取消」，模型会以为没执行过而直接重放有副作用的调用。

    空列表 = 没有悬空（或最后一条回答之后已经有新的对话）。
    """
    wanted: list[tuple[str, str]] = []
    answered: set[str] = set()
    for row in rows:
        blocks = row.content if isinstance(row.content, list) else []
        kind = getattr(row, "kind", KIND_CHAT) or KIND_CHAT
        if kind == KIND_TOOL_RESULT:
            answered.update(str(b.get("tool_use_id") or "") for b in blocks if isinstance(b, dict))
        elif row.role == "assistant":
            wanted = [
                (str(b.get("id") or ""), str(b.get("name") or ""))
                for b in blocks
                if _is_type(b, "tool_use")
            ]
            answered = set()
        else:
            wanted, answered = [], set()  # 用户的新提问之后，前面的悬空不归这一轮管
    return [(call_id, name) for call_id, name in wanted if call_id not in answered]


def unknown_results(call_ids: Sequence[str]) -> StoredMessage:
    """给接管时不能接续的悬空调用补一条「结果未知」：是否执行过、执行到哪一步都不知道，
    如实告诉模型，让它先检查状态再决定是否重试（§5.1）。"""
    return StoredMessage(
        role="user",
        kind=KIND_TOOL_RESULT,
        content=[interrupted_block(call_id, _TAKEOVER_RESULT) for call_id in call_ids],
    )


def answered_calls(rows: Sequence[StoredLike]) -> set[str]:
    """历史里已经有结果的工具调用（tool_result 行里的 tool_use_id）。

    续跑时据此剪掉 waiting_on 里已经不在等的项：检查点会先把结果落进 message 表，
    接管时还会给悬空的调用补「结果未知」。
    """
    answered: set[str] = set()
    for row in rows:
        kind = getattr(row, "kind", KIND_CHAT) or KIND_CHAT
        if kind == KIND_TOOL_RESULT and isinstance(row.content, list):
            answered.update(
                str(b.get("tool_use_id") or "") for b in row.content if _is_type(b, "tool_result")
            )
    return answered
