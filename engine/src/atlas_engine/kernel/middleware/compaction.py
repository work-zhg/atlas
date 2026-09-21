"""上下文自动压缩的接线（文档 §7）。

切分逻辑在 `_compaction.py`，那里刻意不依赖 LangChain。本模块是适配层：
把 LangChain 消息翻译成中立的 MessageFacts、估算 token、在模型调用前触发压缩。

拦截点是 middleware 的 `abefore_model` —— §7.1 要求「每次发起 LLM 调用**前**」
估算。放在 runner 里只能在 run 开始时判断一次，一轮里几十次工具往返
把窗口撑爆就管不到了。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage

from atlas_engine.kernel.middleware._compaction import MessageFacts, drop_thinking, plan_split
from atlas_engine.contracts import ContextOverflow

logger = logging.getLogger(__name__)

#: 摘要回填时的前缀，让模型知道这不是用户说的话
SUMMARY_PREFIX = "【以下是早期对话的摘要】\n"

#: 摘要生成失败时的兜底文本（§7.6：降级为丢弃，但要说明）
DEGRADED_SUMMARY = "【早期对话已省略：摘要生成失败】"

#: 粗略 token 估算的字符除数。
#: 中文约 1 字 1 token，英文约 4 字符 1 token，混合文本取 2.5 偏保守 ——
#: 宁可高估提前压，也不要低估到真超窗口（那是硬 400）。
_CHARS_PER_TOKEN = 2.5


class Summarizer(Protocol):
    """把一段消息压成摘要文本。server 注入 haiku 实现，测试注入假的。"""

    async def __call__(self, messages: Sequence[BaseMessage]) -> str: ...


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # tool_use 的 input 与 tool_result 的 content 都要算进来 ——
                # 它们往往才是撑爆窗口的大头
                for key in ("text", "content", "input"):
                    if key in block:
                        parts.append(str(block[key]))
        return "".join(parts)
    return str(content)


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    """粗估这批消息的 input tokens。

    ★ 刻意不调 count_tokens API：那是一次额外的网络往返，而这里每次模型
      调用前都要算一遍。触发阈值本身留了 25% 余量（§7.1 的 0.75），
      估算误差在这个余量之内不影响正确性。
    """
    chars = sum(len(_text_of(m)) for m in messages)
    return int(chars / _CHARS_PER_TOKEN)


def _blocks(message: BaseMessage) -> list[dict[str, Any]]:
    if not isinstance(message.content, list):
        return []
    return [b for b in message.content if isinstance(b, dict)]


def facts_from(messages: Sequence[BaseMessage]) -> list[MessageFacts]:
    """LangChain 消息 → MessageFacts（compaction.py 的输入）。

    三个判断都容易写错：

    · `starts_turn`：只有**真实用户输入**才算新回合。Anthropic 里 tool_result
      也走 user 角色，把它算成新回合会让回合数虚高，该保留的历史被压掉。
    · `issues`：assistant 发出的 tool_use id。
    · `answers`：tool 消息回应的 id。
    """
    out: list[MessageFacts] = []
    for i, message in enumerate(messages):
        kind = message.type  # human / ai / tool / system
        blocks = _blocks(message)

        issues: set[str] = set()
        if kind == "ai":
            for call in getattr(message, "tool_calls", None) or []:
                if call_id := call.get("id"):
                    issues.add(str(call_id))
            for block in blocks:
                if block.get("type") == "tool_use" and block.get("id"):
                    issues.add(str(block["id"]))

        answers: set[str] = set()
        if kind == "tool" and (call_id := getattr(message, "tool_call_id", None)):
            answers.add(str(call_id))
        for block in blocks:
            if block.get("type") == "tool_result" and block.get("tool_use_id"):
                answers.add(str(block["tool_use_id"]))

        # 只有 human 消息、且不携带 tool_result，才是一个新回合的开始
        starts_turn = kind == "human" and not answers

        thinking_only = bool(blocks) and all(
            b.get("type") in ("thinking", "redacted_thinking") for b in blocks
        )

        out.append(
            MessageFacts(
                index=i,
                starts_turn=starts_turn,
                is_thinking_only=thinking_only,
                issues=frozenset(issues),
                answers=frozenset(answers),
            )
        )
    return out


class CompactionMiddleware(AgentMiddleware):
    """在模型调用前按需压缩历史（§7.1 / §7.2）。"""

    def __init__(
        self,
        *,
        context_window: int,
        trigger_ratio: float,
        keep_recent_turns: int,
        summarizer: Summarizer,
        on_compacted: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """★ 参数全是标量 —— kernel 不认识 CompactionSpec / AgentSpec。

        enabled 也不在这里：关了就别构造（server 的装配层负责），
        装一个自我短路的中间件只会白占一个图节点。
        """
        super().__init__()
        self._window = context_window
        self._trigger_ratio = trigger_ratio
        self._keep_recent_turns = keep_recent_turns
        self._summarize = summarizer
        self._on_compacted = on_compacted

    @property
    def _trigger(self) -> int:
        return int(self._window * self._trigger_ratio)

    async def abefore_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages: list[BaseMessage] = list(state.get("messages") or [])
        if not messages:
            return None

        before = estimate_tokens(messages)
        if before <= self._trigger:
            return None

        facts = facts_from(messages)
        split = plan_split(facts, keep_recent_turns=self._keep_recent_turns)
        if split <= 0:
            # 回合数不够，压不动。§7.6：保留区本身超阈值 → 不再尝试
            raise ContextOverflow(
                f"上下文 {before} tokens 超过阈值 {self._trigger}，"
                f"但最近 {self._keep_recent_turns} 个回合本身就装不下"
            )

        # §7.3 坑 3：压缩区内的 thinking 直接丢，不送去摘要
        dropped = set(drop_thinking(facts, split))
        to_summarize = [m for i, m in enumerate(messages[:split]) if i not in dropped]

        try:
            summary = await self._summarize(to_summarize)
            degraded = False
        except Exception:
            # §7.6：摘要失败降级为「只保留最近回合」，但必须让用户知道
            logger.warning("摘要生成失败，降级为丢弃早期消息", exc_info=True)
            summary = DEGRADED_SUMMARY
            degraded = True

        kept = messages[split:]
        summary_message = AIMessage(content=SUMMARY_PREFIX + summary)
        after = estimate_tokens([summary_message, *kept])

        payload = {
            "messages_summarized": split,
            "tokens_before": before,
            "tokens_after": after,
            "summary": summary,
            "degraded": degraded,
        }
        await self._emit(payload)

        # 用 RemoveMessage 把压缩区从 state 里摘掉，再把摘要插到开头。
        # 注意 message 表不受影响 —— §7.4：模型视角变了，用户视角完整保留。
        removals = [RemoveMessage(id=m.id) for m in messages[:split] if m.id]
        return {"messages": [*removals, summary_message]}

    async def _emit(self, payload: dict[str, Any]) -> None:
        """经 custom stream 通道送出 context.compacted（§7.5 必须让用户看见）。"""
        try:
            from langgraph.config import get_stream_writer

            get_stream_writer()({"kind": "context.compacted", **payload})
        except Exception:  # 图外调用（单测）时没有 writer
            logger.debug("无 stream writer，跳过 context.compacted 事件")

        if self._on_compacted is not None:
            await self._on_compacted(payload)
