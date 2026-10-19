"""上下文管理的纯函数部分（doc/context-management-design.html §7 / §8 / §9.2）。

刻意只依赖 langchain_core 的消息类型、不依赖 middleware 框架 —— 计量、清理计划、工作集提取
都能脱离图单独测。接线在 compaction.py。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

__all__ = [
    "CLEARED_PLACEHOLDER",
    "DEFAULT_CLEARABLE_TOOLS",
    "Composition",
    "TokenMeter",
    "batch_overflow",
    "clear_plan",
    "composition",
    "estimate_text",
    "estimate_tokens",
    "is_context_overflow",
    "is_output_truncated",
    "working_set",
]

#: 粗略 token 估算的字符除数（中文约 1 字 1 token、英文约 4 字符 1 token，取偏保守的 2.5）
CHARS_PER_TOKEN = 2.5

#: 可再生的工具：结果被清掉后，模型能用同样的调用重新拿回来（§8 ②）。
#: ★ 写类工具（write_file / edit_file）的结果本来就短，清了也省不了多少，不在默认清单里。
#: ★ tool_search 不清：已加载的延迟工具是从它的结果里推出来的，清了工具就「消失」了。
DEFAULT_CLEARABLE_TOOLS = frozenset(
    {"read_file", "search", "execute", "knowledge_search", "search_memory"}
)

CLEARED_PLACEHOLDER = "[旧的工具结果已清除以节省上下文；需要时请重新调用 {name} 获取]"

#: 摘要回填时的前缀（与 compaction.SUMMARY_PREFIX 一致，组成统计用它识别摘要消息）
SUMMARY_PREFIX = "【以下是早期对话的摘要】\n"


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
                for key in ("text", "content", "input"):
                    if key in block:
                        parts.append(str(block[key]))
        return "".join(parts)
    return str(content)


def estimate_text(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    """粗估一批消息的 tokens。tool_use 的入参也算进来（往往是大头）。"""
    total = 0
    for m in messages:
        total += len(_text_of(m))
        for call in getattr(m, "tool_calls", None) or []:
            total += len(json.dumps(call.get("args") or {}, ensure_ascii=False, default=str))
    return int(total / CHARS_PER_TOKEN)


@dataclass
class TokenMeter:
    """当前上下文占用的计量（§7）。

    ★ 以最近一次模型调用的 usage 为基线（input 已含 cache_read / cache_creation，output 是那条
      回复本身），加上它之后新增消息的估算。没有基线（新会话首轮、从库里重建的历史不带 usage）
      时退回整体估算 + 已观测到的固定开销（系统提示词 + 工具定义）。
    """

    overhead_tokens: int = 0

    def measure(self, messages: Sequence[BaseMessage]) -> tuple[int, str]:
        """→ (tokens, 口径)。口径：usage | estimate。"""
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            usage = getattr(m, "usage_metadata", None) if isinstance(m, AIMessage) else None
            if usage and usage.get("input_tokens"):
                base = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                return base + estimate_tokens(messages[i + 1 :]), "usage"
        return estimate_tokens(messages) + self.overhead_tokens, "estimate"


def effective_window(context_window: int, max_output_tokens: int) -> int:
    """有效窗口 = 窗口 − min(最大输出, 20k)：给回复与压缩时的摘要输出留位置。"""
    return max(1, context_window - min(max_output_tokens, 20_000))


# ─────────────────────────────────────────────── 清理旧工具结果（§8 ②）


def _call_names(messages: Sequence[BaseMessage]) -> dict[str, str]:
    """tool_call_id → 工具名（ToolMessage.name 有时为空，以发起调用的 AIMessage 为准）。"""
    out: dict[str, str] = {}
    for m in messages:
        for call in getattr(m, "tool_calls", None) or []:
            if call.get("id"):
                out[str(call["id"])] = str(call.get("name") or "")
    return out


def clear_plan(
    messages: Sequence[BaseMessage],
    *,
    clearable: Iterable[str],
    keep_recent: int,
) -> list[ToolMessage]:
    """要清掉的旧工具结果 → 替换后的 ToolMessage（同 id，按 id 去重的 reducer 会原地替换）。

    ★ 最后一批（最近一条 AIMessage 之后的）结果从不清：模型马上就要读它们。
    ★ 已经是占位的不重复清。
    """
    clearable = set(clearable)
    names = _call_names(messages)
    last_ai = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=-1)
    candidates: list[ToolMessage] = []
    for i, m in enumerate(messages):
        if i > last_ai or not isinstance(m, ToolMessage) or not m.id:
            continue
        name = names.get(str(m.tool_call_id)) or (m.name or "")
        if name not in clearable:
            continue
        text = _text_of(m)
        if text.startswith("[旧的工具结果已清除"):
            continue
        candidates.append(m)
    victims = candidates[: max(0, len(candidates) - keep_recent)]
    return [
        ToolMessage(
            content=CLEARED_PLACEHOLDER.format(
                name=names.get(str(m.tool_call_id)) or m.name or "该工具"
            ),
            tool_call_id=m.tool_call_id,
            name=m.name,
            id=m.id,
            status=getattr(m, "status", "success"),
        )
        for m in victims
    ]


def batch_overflow(messages: Sequence[BaseMessage], cap_tokens: int) -> list[ToolMessage]:
    """最后一批工具结果总量超过上限时，从最大的开始挑出要外置的（§8 ①）。"""
    batch: list[ToolMessage] = []
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            break
        batch.append(m)
    sizes = [(estimate_text(_text_of(m)), m) for m in batch]
    total = sum(s for s, _ in sizes)
    picked: list[ToolMessage] = []
    for size, m in sorted(sizes, key=lambda x: -x[0]):
        if total <= cap_tokens:
            break
        picked.append(m)
        total -= size
    return picked


# ─────────────────────────────────────────────── 压缩后的工作集（§9.2）


_FILE_TOOLS = {"read_file", "write_file", "edit_file"}


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
             and not p.startswith("/large_tool_results/")][:max_files]  # fmt: skip
    return files, skills


# ─────────────────────────────────────────────── 失败识别（§8 ⑤）


_OVERFLOW_HINTS = (
    "prompt is too long",
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "context window",
    "too many tokens",
    "input is too long",
    "reduce the length",
)


def is_context_overflow(exc: BaseException) -> bool:
    """模型网关返回「上下文过长」—— 各家措辞不同，按关键词认。只认 4xx。"""
    status = getattr(exc, "status_code", None)
    if status is not None and not (isinstance(status, int) and 400 <= status < 500):
        return False
    text = str(exc).lower()
    return any(h in text for h in _OVERFLOW_HINTS)


def is_output_truncated(message: BaseMessage) -> bool:
    """回复因为达到输出上限被截断（Anthropic: stop_reason=max_tokens；OpenAI: finish_reason=length）。"""
    meta = getattr(message, "response_metadata", None) or {}
    return meta.get("stop_reason") == "max_tokens" or meta.get("finish_reason") == "length"


# ─────────────────────────────────────────────── 构成（§16）


@dataclass
class Composition:
    system: int = 0
    tools: int = 0
    summary: int = 0
    conversation: int = 0
    tool_results: int = 0
    attachments: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)

    @property
    def total(self) -> int:
        return sum(self.__dict__.values())


def composition(
    *,
    system_text: str,
    tools: Sequence[Any],
    messages: Sequence[BaseMessage],
    attachment_tokens: int,
) -> Composition:
    c = Composition(system=estimate_text(system_text), attachments=attachment_tokens)
    for t in tools:
        try:
            from langchain_core.utils.function_calling import convert_to_openai_tool

            schema = t if isinstance(t, dict) else convert_to_openai_tool(t)
            c.tools += estimate_text(json.dumps(schema, ensure_ascii=False, default=str))
        except Exception:  # 某些工具无法转换 schema：按描述估
            c.tools += estimate_text(str(getattr(t, "description", "")))
    for m in messages:
        n = estimate_tokens([m])
        if isinstance(m, ToolMessage):
            c.tool_results += n
        elif isinstance(m, AIMessage) and _text_of(m).startswith(SUMMARY_PREFIX):
            c.summary += n
        else:
            c.conversation += n
    return c
