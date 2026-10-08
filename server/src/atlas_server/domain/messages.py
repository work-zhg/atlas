"""LC 消息 ⇄ 存储 blocks 的双向映射。

★ 为什么需要它：一轮的**中间消息**也要能落库再取回 —— 模型发出的 tool_use、
  以及每个工具的结果。原先只落最终正文，于是下一轮重建历史时这些全不存在。
  单轮内没问题（消息活在 LangGraph 的 state 里），但一旦一轮要分成多段执行
  （委派挂起后续跑），段之间的唯一载体就是 message 表。

  而 Anthropic 的硬要求是：带 tool_use 的 assistant turn 之后**必须**有每个
  tool_use_id 对应的 tool_result，缺一个就是 400。所以「存完整序列」不是
  优化，是分段执行能不能成立的前提。

★ 存储格式是 **Anthropic 原生 content blocks**，不是 LangChain 的序列化形式。
  message 表的承诺一直是「存 Anthropic content blocks 原始结构」（§7.4），
  而存 LC 的 model_dump 会把这张要留几年的表绑在 LangChain 的版本上 ——
  升一次大版本就要写一次数据迁移。

★ role 枚举因此一个字都不用改：Anthropic 的 tool_result 本来就是 **user**
  角色的 block，tool_use 是 assistant 的 block。区分靠 `kind` 列。

★ 纯计算层：只认 langchain_core 的消息类型，不碰 DB / ORM。`from_storage`
  接受任何有 role / kind / content 三个属性的对象（repository 传 ORM 行，
  测试传 dataclass）—— 这样映射逻辑能脱离数据库单测。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

__all__ = [
    "KIND_CHAT",
    "KIND_TOOL_RESULT",
    "StoredMessage",
    "Transcript",
    "from_storage",
    "to_storage",
]

#: 对话轮：用户的提问、助手的回答（后者可携带 tool_use blocks）。
KIND_CHAT = "chat"

#: 工具结果：role=user，content 是 tool_result blocks。
#:
#: ★ 必须与 chat 分开。它也是 role=user，而「最后一条 user 消息是本轮输入」
#:   这条判据不加区分就会把一批工具结果当成用户的新提问 —— 表现是模型莫名
#:   回答一段 JSON，而用户的问题被当成历史。
KIND_TOOL_RESULT = "tool_result"

#: 这些 block 类型原样保留在 assistant 的 content 里。
#:
#: ★ thinking 必须留着：开了 extended thinking 时 Anthropic 要求带 tool_use 的
#:   assistant turn 把 thinking block 原样回传。丢了轻则降级、重则 400，而
#:   两种都难查 —— 报错指向的是「消息格式」，根因却在两个 run 之前的落库。
_PRESERVED_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})


class _StoredLike(Protocol):
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


# --------------------------------------------------------------------------- 出


def to_storage(messages: Sequence[BaseMessage]) -> list[StoredMessage]:
    """LC 消息序列 → 待落库的行。

    ★ 连续的 ToolMessage 被**合并成一条** user/tool_result。Anthropic 要求
      一批 tool_result 处在同一个 user turn 里；拆成多条 user 消息的话，
      模型侧看到的是「工具结果之间插了几轮空对话」，而多数 provider 直接报
      格式错误。

    ★ 返回的序列保证**每个 tool_use 都有配对的 tool_result** —— 见
      `_seal_dangling_tool_calls`。这是本函数的不变量，不是调用方的责任：
      忘了补的代价发生在几分钟后的另一个 run 里，而报错只会说「消息格式
      不对」。
    """
    return _seal_dangling_tool_calls(_map_messages(messages))


def _map_messages(messages: Sequence[BaseMessage]) -> list[StoredMessage]:
    out: list[StoredMessage] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append(
                StoredMessage(role="user", kind=KIND_TOOL_RESULT, content=list(pending_results))
            )
            pending_results.clear()

    for message in messages:
        if isinstance(message, ToolMessage):
            pending_results.append(_tool_result_block(message))
            continue

        flush_results()

        if isinstance(message, AIMessage):
            blocks = _preserved_blocks(message.content)
            # ★ tool_use block 由 tool_calls **合成**，不从 content 里捞。
            #   Anthropic 方言下 content 里本来就有一份，OpenAI 方言下则
            #   完全没有（tool_calls 走独立字段）。统一从 tool_calls 生成，
            #   两种方言落库后长相一致 —— 否则同一个会话换个模型就读不回来。
            blocks.extend(_tool_use_block(call) for call in message.tool_calls or [])
            if blocks:
                out.append(StoredMessage(role="assistant", kind=KIND_CHAT, content=blocks))
        elif isinstance(message, HumanMessage):
            blocks = _preserved_blocks(message.content)
            if blocks:
                out.append(StoredMessage(role="user", kind=KIND_CHAT, content=blocks))
        # 其它类型（SystemMessage / RemoveMessage）不落库：system 每轮由 spec
        # 重新拼（build.py 是唯一装配点），RemoveMessage 是压缩的内部指令。

    flush_results()
    return out


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
_INTERRUPTED_RESULT = "工具执行被中断，结果未知（上一轮未正常结束）。"


def _seal_dangling_tool_calls(stored: list[StoredMessage]) -> list[StoredMessage]:
    """给每个没有配对结果的 tool_use 补一条 is_error 的 tool_result。"""
    out: list[StoredMessage] = []
    index = 0
    while index < len(stored):
        item = stored[index]
        out.append(item)
        index += 1

        wanted = [
            str(block.get("id") or "")
            for block in item.content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if item.role != "assistant" or not wanted:
            continue

        following = stored[index] if index < len(stored) else None
        if following is not None and following.kind == KIND_TOOL_RESULT:
            satisfied = {
                block.get("tool_use_id")
                for block in following.content
                if isinstance(block, dict)
            }
            missing = [call_id for call_id in wanted if call_id not in satisfied]
            if missing:
                # 补进**已有的那一批**，而不是另起一条 —— 同一批 tool_result
                # 必须在同一个 user turn 里。
                stored[index] = StoredMessage(
                    role=following.role,
                    kind=following.kind,
                    content=[*following.content, *(_interrupted_block(i) for i in missing)],
                )
            continue

        # 后面根本没有结果那一条（一轮在工具返回前就断了）—— 整批都要补。
        out.append(
            StoredMessage(
                role="user",
                kind=KIND_TOOL_RESULT,
                content=[_interrupted_block(call_id) for call_id in wanted],
            )
        )
    return out


def _interrupted_block(call_id: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": _INTERRUPTED_RESULT,
        "is_error": True,
        "atlas_name": "",
    }


def _preserved_blocks(content: Any) -> list[dict[str, Any]]:
    """content → 可原样保留的 blocks。str 形态包成一个 text block。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    return [
        dict(block)
        for block in content
        if isinstance(block, dict) and block.get("type") in _PRESERVED_BLOCKS
    ]


def _tool_use_block(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": call.get("id") or "",
        "name": call.get("name") or "",
        "input": call.get("args") or {},
    }


def _tool_result_block(message: ToolMessage) -> dict[str, Any]:
    content = message.content
    return {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": content if isinstance(content, str) else str(content),
        "is_error": message.status == "error",
        # ★ name 是**非标准字段**，Anthropic 的 tool_result 没有它的位置。
        #   留着是因为工具名是事件流与审批里最常用的标识，而重建出的
        #   ToolMessage 一旦没有 name，「这条结果是谁的」就只能靠 id 反查
        #   上一条 assistant 消息。provider 会忽略不认识的键（实测），
        #   而我们自己读得到 —— 这个取舍比丢掉工具名划算。
        "atlas_name": message.name or "",
    }


# --------------------------------------------------------------------------- 入


def from_storage(rows: Sequence[_StoredLike]) -> list[BaseMessage]:
    """存储行 → LC 消息序列。`to_storage` 的逆。

    ★ 重建出的 AIMessage 的 content 里**不放** tool_use block，工具调用只体现
      在 `tool_calls` 字段上。那是 LangChain 的标准路径，两种方言的 adapter
      都认；而把 tool_use block 塞进 content 只有 Anthropic 方言认得，换成
      OpenAI 方言的模型就会把它当成一段无意义的结构体发出去。
    """
    out: list[BaseMessage] = []
    for row in rows:
        blocks = row.content if isinstance(row.content, list) else []
        kind = getattr(row, "kind", KIND_CHAT) or KIND_CHAT

        if kind == KIND_TOOL_RESULT:
            out.extend(_tool_messages(blocks))
            continue

        if row.role == "assistant":
            out.append(
                AIMessage(
                    content=[b for b in blocks if _is_preserved(b)],
                    tool_calls=[_tool_call(b) for b in blocks if _is_type(b, "tool_use")],
                )
            )
        else:
            out.append(HumanMessage(content=[b for b in blocks if _is_preserved(b)]))
    return out


def _tool_messages(blocks: Sequence[Any]) -> list[ToolMessage]:
    out: list[ToolMessage] = []
    for block in blocks:
        if not _is_type(block, "tool_result"):
            continue
        out.append(
            ToolMessage(
                content=block.get("content") or "",
                tool_call_id=block.get("tool_use_id") or "",
                name=block.get("atlas_name") or None,
                status="error" if block.get("is_error") else "success",
            )
        )
    return out


def _tool_call(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "id": block.get("id") or "",
        "name": block.get("name") or "",
        "args": block.get("input") or {},
    }


def _is_type(block: Any, type_: str) -> bool:
    return isinstance(block, dict) and block.get("type") == type_


def _is_preserved(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _PRESERVED_BLOCKS


# --------------------------------------------------------------------------- sink


@dataclass
class Transcript:
    """一轮里模型与工具**实际产生**的消息，按发生顺序。

    ★ 为什么是出参而不是从事件流重建：事件流是给人看的投影。正文按
      `Answer.seal()` 的时机分段，而 seal 只在「当前段非空」时开新段 ——
      模型连着调两批工具、中间没说话时，段与 tool_use 的交织顺序就再也
      反推不出来。用它重建的消息序列会在某些模型行为下静默错位，而报错
      要等到下一段执行时 provider 回 400。

    ★ 只收 model / tools 两个节点的输出，不收中间件节点的。压缩中间件会
      改写 state 里的 messages（驱逐、换成摘要），那是**模型视角**的变化；
      落库的必须是原始全量（「压缩只改变模型视角，原始消息一条不丢」）。
    """

    messages: list[BaseMessage] = field(default_factory=list)

    def extend(self, messages: Sequence[BaseMessage]) -> None:
        self.messages.extend(messages)

    def to_storage(self) -> list[StoredMessage]:
        return to_storage(self.messages)

    def __bool__(self) -> bool:
        return bool(self.messages)
