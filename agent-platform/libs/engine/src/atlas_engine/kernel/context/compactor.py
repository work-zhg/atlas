"""自动压缩（c4-model/agent/native-runtime/code-native-runtime-context.html）。

在完整回合的边界上切（tool_use 和 tool_result 必须成对；一个回合太长时在回合内找安全点），
切出的部分丢掉思考块后交给摘要器，再按预算恢复最近用过的工作区文件和技能，最后交回按顺序
重写的消息。摘要和覆盖边界经 on_compacted 存进会话的上下文状态；连续失败 N 次就熔断。

切分的纯函数在 `_split.py`，本模块是它和摘要器、工作区之间的接线。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from atlas_engine.contracts import ContextOverflow
from atlas_engine.kernel.context._split import (
    MessageFacts,
    drop_thinking,
    plan_split,
    plan_split_within_turn,
)
from atlas_engine.kernel.context._text import SUMMARY_PREFIX, emit, estimate_text, estimate_tokens
from atlas_engine.kernel.context.offload import OFFLOAD_DIR

logger = logging.getLogger(__name__)

__all__ = ["DEGRADED_SUMMARY", "Compactor", "Summarizer", "facts_from", "working_set"]

#: 摘要生成失败时的兜底文本（降级为丢弃，但要说明）
DEGRADED_SUMMARY = "【早期对话已省略：摘要生成失败】"
#: 工作集恢复段的标题
_RESTORE_HEADER = "\n\n【压缩前正在处理的内容（已重新读取当前版本）】"
#: 回合内压缩时保留最近几轮工具调用（一轮 = 一条发出 tool_use 的 assistant 及其结果）
_KEEP_RECENT_ROUNDS = 3
#: 回合内压缩后的衔接（平台附加，不是用户说的话）
_CONTINUE_NOTE = (
    "（以下为平台附加的说明，不是用户说的话）上文的早期过程已压缩为摘要，"
    "用户的原始请求见摘要。请从当前进度继续，不要重复已完成的步骤。"
)
_FILE_TOOLS = {"read_file", "write_file", "edit_file"}


class Summarizer(Protocol):
    """把一段消息压成摘要文本。server 注入实现，测试注入假的。"""

    async def __call__(self, messages: Sequence[BaseMessage]) -> str: ...


def _blocks(message: BaseMessage) -> list[dict[str, Any]]:
    if not isinstance(message.content, list):
        return []
    return [b for b in message.content if isinstance(b, dict)]


def facts_from(messages: Sequence[BaseMessage]) -> list[MessageFacts]:
    """LangChain 消息 → MessageFacts（切分逻辑的输入）。

    · `starts_turn`：只有**真实用户输入**才算新回合（tool_result 也走 user 角色）。
    · `issues` / `answers`：tool_use 的 id 与 tool_result 回应的 id，保证不拆对。
    """
    out: list[MessageFacts] = []
    for i, message in enumerate(messages):
        kind = message.type
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


def working_set(messages: Sequence[BaseMessage], *, max_files: int) -> tuple[list[str], list[str]]:
    """被压缩区间里最近用过的 (工作区文件, 技能文件)，按最近使用排序、去重。"""
    seen: dict[str, int] = {}
    order = 0
    for m in messages:
        for call in getattr(m, "tool_calls", None) or []:
            if call.get("name") not in _FILE_TOOLS:
                continue
            path = str((call.get("args") or {}).get("file_path") or "")
            if path:
                order += 1
                seen[path] = order
    ranked = [p for p, _ in sorted(seen.items(), key=lambda kv: -kv[1])]
    skills = [p for p in ranked if p.startswith("/skills/") and p.endswith("SKILL.md")]
    files = [p for p in ranked if not p.startswith("/skills/")
             and not p.startswith(f"{OFFLOAD_DIR}/")][:max_files]  # fmt: skip
    return files, skills


class Compactor:
    """压缩一段消息。failure_count 是会话里已经连续失败的次数（从上下文状态读出来的）。"""

    def __init__(
        self,
        *,
        summarizer: Summarizer,
        keep_recent_turns: int,
        workspace: Any = None,
        restore_files: int = 5,
        restore_file_tokens: int = 5_000,
        restore_total_tokens: int = 40_000,
        restore_skill_tokens: int = 20_000,
        failure_count: int = 0,
        max_failures: int = 3,
        on_compacted: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_failed: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        """★ 参数全是标量 / 回调 —— kernel 不认识 AgentSpec。"""
        self._summarize = summarizer
        self._keep_recent_turns = keep_recent_turns
        self._fs = workspace
        self._restore_files = restore_files
        self._restore_file_tokens = restore_file_tokens
        self._restore_total = restore_total_tokens
        self._restore_skills = restore_skill_tokens
        self._failures = failure_count
        self._max_failures = max_failures
        self._on_compacted = on_compacted
        self._on_failed = on_failed

    @property
    def circuit_open(self) -> bool:
        """连续失败到了上限：不再自动压缩（反应式的强制压缩不受影响）。"""
        return self._failures >= self._max_failures

    async def compact(
        self,
        messages: list[BaseMessage],
        *,
        tokens: int,
        reason: str,
        measure: Callable[[Sequence[BaseMessage]], int] = estimate_tokens,
    ) -> list[BaseMessage]:
        """→ 压缩后的完整消息（摘要在最前）。切不动时抛 ContextOverflow。

        reason：threshold（到了压缩档）| overflow（模型说上下文过长）。measure 量压缩后的占用。
        """
        facts = facts_from(messages)
        split = plan_split(facts, keep_recent_turns=self._keep_recent_turns)
        intra_turn = False
        if split <= 0:
            # 回合数不够（典型：单轮长任务）→ 退到回合内按工具轮次切
            split = plan_split_within_turn(facts, keep_recent_rounds=_KEEP_RECENT_ROUNDS)
            intra_turn = True
        if split <= 0:
            # 连最近几轮工具调用都装不下：报错而不是无限压缩
            raise ContextOverflow(
                f"上下文 {tokens} tokens 需要压缩，"
                f"但最近 {_KEEP_RECENT_ROUNDS} 轮工具调用本身就装不下"
            )
        dropped = set(drop_thinking(facts, split))
        head = messages[:split]
        to_summarize = [m for i, m in enumerate(head) if i not in dropped]
        try:
            summary = await self._summarize(to_summarize)
            degraded = False
            self._failures = 0
        except Exception:
            logger.warning("摘要生成失败，降级为丢弃早期消息", exc_info=True)
            summary = DEGRADED_SUMMARY
            degraded = True
            self._failures += 1
            if self._on_failed is not None:
                await self._on_failed(self._failures)

        restore_text, restored = await self._restore_working_set(head)
        kept = messages[split:]
        summary_message = AIMessage(content=SUMMARY_PREFIX + summary + restore_text)
        after_messages: list[BaseMessage] = [summary_message, *kept]
        if kept and not isinstance(kept[0], HumanMessage):
            # 回合内切分：保留区从 assistant 开始，紧跟在摘要（也是 assistant）后面。
            # 补一条标明来源的衔接，避免两条 assistant 相连、也让模型知道是「接着做」
            after_messages.insert(1, HumanMessage(content=_CONTINUE_NOTE))
        payload = {
            "kind": "context.compacted",
            "reason": reason,
            "messages_summarized": split,
            "tokens_before": tokens,
            "tokens_after": measure(after_messages),
            "summary": summary,
            "degraded": degraded,
            "restored_files": restored,
            "intra_turn": intra_turn,
            "failures": self._failures,
            "circuit_open": self.circuit_open,
        }
        emit(payload)
        # ★ 回合内的摘要不落库：它覆盖的是本轮内部的过程，而本轮完整的历史会在结束时
        #   照常落库 —— 落了的话下一轮会「摘要 + 原文」重复。下一轮有了新的用户回合，
        #   就能按回合正常压缩。
        if self._on_compacted is not None and not degraded and not intra_turn:
            await self._on_compacted({k: v for k, v in payload.items() if k != "kind"})
        return after_messages

    async def _restore_working_set(self, head: Sequence[BaseMessage]) -> tuple[str, list[str]]:
        """压缩后恢复工作集：被压缩区间里最近用过的文件、技能，重新读当前版本。"""
        if self._fs is None or self._restore_files <= 0:
            return "", []
        files, skills = working_set(head, max_files=self._restore_files)
        parts: list[str] = []
        restored: list[str] = []
        budget = self._restore_total
        skill_budget = self._restore_skills
        for path, is_skill in [*((p, False) for p in files), *((p, True) for p in skills)]:
            cap = min(self._restore_file_tokens, skill_budget if is_skill else budget)
            if cap <= 200:
                continue
            try:
                result = await self._fs.aread(path, offset=0, limit=2_000)
            except Exception:
                continue
            data = getattr(result, "file_data", None) or {}
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, str) or not content:
                continue
            chars = int(cap * 2.5)
            body = content if len(content) <= chars else content[:chars] + "\n…（已截断）"
            parts.append(f"\n--- {path} ---\n{body}")
            used = estimate_text(body)
            if is_skill:
                skill_budget -= used
            else:
                budget -= used
            restored.append(path)
        if not parts:
            return "", []
        return _RESTORE_HEADER + "".join(parts), restored
