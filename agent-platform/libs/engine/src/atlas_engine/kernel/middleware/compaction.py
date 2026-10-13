"""上下文管理中间件（doc/context-management-design.html；原「自动压缩」§7 的扩展）。

纯函数在 `_context.py`（计量、清理计划、工作集）与 `_compaction.py`（切分），本模块是接线：

    abefore_model（模型调用前，改 state —— 本段后续调用都看到）
      ⓪ 应用上一次反应式压缩的结果
      ① 最后一批工具结果总量封顶：最大的几条外置到工作区
      ② 旧工具结果清理：占用 ≥ 清理档，或距上次回复已超过缓存 TTL（缓存反正失效）
      ③ 自动压缩：占用 ≥ 压缩档 → 结构化摘要 + 工作集恢复（连续失败 N 次熔断）

    awrap_model_call（这一次调用，不改 state）
      ④ 附件：<system-reminder> 追加在最后（记忆、用量提醒、待办提醒……），不落库、不进历史
      ⑤ 失败恢复：上下文过长 → 强制压缩后重试一次；输出被截断 → 提高输出上限重试一次
      ⑥ 构成观测：context.usage 事件（各层 token、实际 usage、档位）

★ 所有改写只作用于模型视角（graph state）；message 表不受影响（§7.4）。
★ 压缩写回 state 用「清空 + 按序重写」：只删压缩区再追加摘要的话，reducer 把摘要追加在**末尾**，
  模型看到的是 [最近消息…, 摘要] —— 最后一条是 assistant，下一次调用就成了对摘要的续写。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Protocol

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from atlas_engine.contracts import ContextOverflow
from atlas_engine.kernel.middleware._compaction import MessageFacts, drop_thinking, plan_split
from atlas_engine.kernel.middleware._context import (
    DEFAULT_CLEARABLE_TOOLS,
    SUMMARY_PREFIX,
    TokenMeter,
    batch_overflow,
    clear_plan,
    composition,
    effective_window,
    estimate_text,
    is_context_overflow,
    is_output_truncated,
    working_set,
)
from atlas_engine.kernel.middleware._context import (
    estimate_tokens as estimate_tokens,  # 兼容旧的 import 路径
)

logger = logging.getLogger(__name__)

__all__ = ["DEGRADED_SUMMARY", "SUMMARY_PREFIX", "CompactionMiddleware", "Summarizer",
           "estimate_tokens", "facts_from"]  # fmt: skip

#: 摘要生成失败时的兜底文本（§7.6：降级为丢弃，但要说明）
DEGRADED_SUMMARY = "【早期对话已省略：摘要生成失败】"
#: 工作集恢复段的标题
_RESTORE_HEADER = "\n\n【压缩前正在处理的内容（已重新读取当前版本）】"
_LARGE_RESULTS_DIR = "/large_tool_results"


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


def _emit(payload: dict[str, Any]) -> None:
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:  # 图外调用（单测）时没有 writer
        logger.debug("无 stream writer，跳过 %s 事件", payload.get("kind"))


def _text(message: BaseMessage) -> str:
    c = message.content
    if isinstance(c, str):
        return c
    return "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in (c or []))


class CompactionMiddleware(AgentMiddleware):
    """模型调用前后的上下文管理（§8 流水线）。"""

    def __init__(
        self,
        *,
        context_window: int,
        trigger_ratio: float,
        keep_recent_turns: int,
        summarizer: Summarizer,
        on_compacted: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        max_output_tokens: int = 0,
        clear_ratio: float | None = None,
        warn_ratio: float | None = None,
        keep_recent_tool_results: int = 5,
        clearable_tools: Iterable[str] = DEFAULT_CLEARABLE_TOOLS,
        batch_cap_tokens: int | None = None,
        idle_s: float | None = None,
        cache_ttl_s: float | None = None,
        filesystem: Any = None,
        restore_files: int = 5,
        restore_file_tokens: int = 5_000,
        restore_total_tokens: int = 40_000,
        restore_skill_tokens: int = 20_000,
        failure_count: int = 0,
        max_failures: int = 3,
        on_failed: Callable[[int], Awaitable[None]] | None = None,
        max_output_cap: int | None = None,
        attachments: Sequence[tuple[str, str]] = (),
        attachment_budget_tokens: int | None = None,
        todo_reminder_after: int = 8,
    ) -> None:
        """★ 参数全是标量 / 回调 —— kernel 不认识 AgentSpec。关了就别构造（server 负责）。"""
        super().__init__()
        self._window = context_window
        self._effective = effective_window(context_window, max_output_tokens)
        self._trigger_ratio = trigger_ratio
        self._clear_ratio = clear_ratio
        self._warn_ratio = warn_ratio
        self._keep_recent_turns = keep_recent_turns
        self._keep_tool_results = keep_recent_tool_results
        self._clearable = frozenset(clearable_tools)
        self._batch_cap = batch_cap_tokens
        self._idle_s = idle_s
        self._cache_ttl_s = cache_ttl_s
        self._fs = filesystem
        self._restore_files = restore_files
        self._restore_file_tokens = restore_file_tokens
        self._restore_total = restore_total_tokens
        self._restore_skills = restore_skill_tokens
        self._failures = failure_count
        self._max_failures = max_failures
        self._on_failed = on_failed
        self._max_output_cap = max_output_cap
        self._attachments = list(attachments)
        self._attachment_budget = attachment_budget_tokens or max(1_000, self._effective // 10)
        self._todo_after = todo_reminder_after
        self._summarize = summarizer
        self._on_compacted = on_compacted
        self._meter = TokenMeter()
        self._idle_checked = False
        #: 反应式压缩的结果：(压缩后的消息, 压缩时 state 的长度)，下一个 before_model 写回 state
        self._pending: tuple[list[BaseMessage], int] | None = None

    # ------------------------------------------------------------------ 阈值

    def _threshold(self, ratio: float | None) -> int | None:
        return int(self._effective * ratio) if ratio else None

    @property
    def _trigger(self) -> int:
        return int(self._effective * self._trigger_ratio)

    # ------------------------------------------------------------------ before_model

    async def abefore_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages: list[BaseMessage] = list(state.get("messages") or [])
        if not messages:
            return None
        rewrite = False  # 需要「清空 + 按序重写」（压缩）还是只需按 id 替换

        # ⓪ 上一次调用里发生了反应式压缩：把结果写进 state（之后新增的消息接在后面）
        if self._pending is not None:
            compacted, old_len = self._pending
            self._pending = None
            messages = [*compacted, *messages[old_len:]]
            rewrite = True

        replaced: dict[str, BaseMessage] = {}

        # ① 最后一批工具结果总量封顶
        if self._batch_cap:
            for m in batch_overflow(messages, self._batch_cap):
                new = await self._offload(m)
                replaced[str(m.id)] = new
            messages = [replaced.get(str(m.id), m) for m in messages]

        # ② 旧工具结果清理
        tokens, _basis = self._meter.measure(messages)
        clear_at = self._threshold(self._clear_ratio)
        idle_trigger = False
        if not self._idle_checked:
            self._idle_checked = True
            idle_trigger = bool(
                self._idle_s and self._cache_ttl_s and self._idle_s >= self._cache_ttl_s
            )
        if (clear_at and tokens >= clear_at) or idle_trigger:
            plan = clear_plan(
                messages, clearable=self._clearable, keep_recent=self._keep_tool_results
            )
            if plan:
                freed = sum(
                    estimate_text(_text(m))
                    for m in messages
                    if str(m.id) in {str(p.id) for p in plan}
                ) - sum(estimate_text(_text(p)) for p in plan)
                replaced.update({str(p.id): p for p in plan})
                messages = [replaced.get(str(m.id), m) for m in messages]
                tokens = max(0, tokens - freed)
                _emit({
                    "kind": "context.cleared",
                    "cleared": len(plan),
                    "tokens_freed": freed,
                    "reason": "idle" if idle_trigger and not (clear_at and tokens >= clear_at)
                    else "threshold",
                })  # fmt: skip

        # ③ 自动压缩
        if tokens > self._trigger and self._failures < self._max_failures:
            messages, _payload = await self._compact(messages, tokens, reason="threshold")
            rewrite = True

        if rewrite:
            return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]}
        if replaced:
            return {"messages": list(replaced.values())}
        return None

    async def _offload(self, message: ToolMessage) -> ToolMessage:
        """把一条大结果外置到工作区，消息里留预览 + 路径（§8 ①）。"""
        text = _text(message)
        path = f"{_LARGE_RESULTS_DIR}/{message.tool_call_id}.txt"
        note = ""
        if self._fs is not None:
            try:
                await self._fs.awrite(path, text)
                note = f"完整内容已保存到 {path}，需要时用 read_file 分页读取。"
            except Exception:
                logger.warning("外置工具结果失败，改为截断", exc_info=True)
        if not note:
            note = "内容过长已截断（未能保存完整内容）。"
        preview = text[:2_000]
        return ToolMessage(
            content=f"[本批工具结果总量过大，此条已外置] {note}\n预览（前 2000 字符）：\n{preview}",
            tool_call_id=message.tool_call_id,
            name=message.name,
            id=message.id,
            status=getattr(message, "status", "success"),
        )

    # ------------------------------------------------------------------ 压缩

    async def _compact(
        self, messages: list[BaseMessage], before: int, *, reason: str
    ) -> tuple[list[BaseMessage], dict[str, Any]]:
        facts = facts_from(messages)
        split = plan_split(facts, keep_recent_turns=self._keep_recent_turns)
        if split <= 0:
            # 回合数不够，压不动（§7.6：保留区本身就装不下 → 报错而不是无限压缩）
            raise ContextOverflow(
                f"上下文 {before} tokens 超过阈值 {self._trigger}，"
                f"但最近 {self._keep_recent_turns} 个回合本身就装不下"
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

        restore_text, restored = await self._restore(head)
        kept = messages[split:]
        summary_message = AIMessage(content=SUMMARY_PREFIX + summary + restore_text)
        after_messages = [summary_message, *kept]
        after = self._meter.measure(after_messages)[0]
        payload = {
            "kind": "context.compacted",
            "reason": reason,
            "messages_summarized": split,
            "tokens_before": before,
            "tokens_after": after,
            "summary": summary,
            "degraded": degraded,
            "restored_files": restored,
            "failures": self._failures,
            "circuit_open": self._failures >= self._max_failures,
        }
        _emit(payload)
        if self._on_compacted is not None and not degraded:
            await self._on_compacted({k: v for k, v in payload.items() if k != "kind"})
        return after_messages, payload

    async def _restore(self, head: Sequence[BaseMessage]) -> tuple[str, list[str]]:
        """压缩后恢复工作集：被压缩区间里最近用过的文件、技能，重新读当前版本（§9.2）。"""
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

    # ------------------------------------------------------------------ wrap_model_call

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        system_text = _text(request.system_message) if request.system_message is not None else ""
        comp = composition(
            system_text=system_text,
            tools=list(request.tools or []),
            messages=list(request.messages),
            attachment_tokens=0,
        )
        self._meter.overhead_tokens = comp.system + comp.tools
        tokens, basis = self._meter.measure(list(request.messages))
        reminders = self._reminders(request, tokens)
        att_tokens = sum(estimate_text(t) for _, t in reminders)
        req = self._with_attachments(request, reminders)

        try:
            resp = await handler(req)
        except Exception as exc:
            if not is_context_overflow(exc) or self._pending is not None:
                raise
            # ⑤ 上下文过长：强制压缩后重试一次；结果在下一个 before_model 写回 state
            logger.warning("模型返回上下文过长，强制压缩后重试：%s", str(exc)[:200])
            compacted, _ = await self._compact(list(request.messages), tokens, reason="overflow")
            self._pending = (compacted, len(request.messages))
            req = self._with_attachments(request.override(messages=compacted), reminders)
            resp = await handler(req)

        ai = _last_ai(resp)
        if ai is not None and is_output_truncated(ai) and self._max_output_cap:
            current = int(
                request.model_settings.get("max_tokens")
                or getattr(request.model, "max_tokens", 0)
                or 0
            )
            raised = min(self._max_output_cap, max(current * 2, 8_192))
            if raised > current:
                logger.info("输出被截断（max_tokens=%d），提高到 %d 重试一次", current, raised)
                resp = await handler(
                    req.override(model_settings={**request.model_settings, "max_tokens": raised})
                )
                ai = _last_ai(resp)

        usage = getattr(ai, "usage_metadata", None) or {} if ai is not None else {}
        comp.attachments = att_tokens
        _emit({
            "kind": "context.usage",
            "tokens": int(usage.get("input_tokens") or 0) or tokens + att_tokens,
            "basis": "usage" if usage.get("input_tokens") else basis,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "context_window": self._window,
            "effective_window": self._effective,
            "thresholds": {
                "clear": self._threshold(self._clear_ratio),
                "compact": self._trigger,
                "warn": self._threshold(self._warn_ratio),
            },
            "composition": comp.as_dict(),
            "attachments": [n for n, _ in reminders],
            "compaction_disabled": self._failures >= self._max_failures,
        })  # fmt: skip
        return resp

    def _reminders(self, request: Any, tokens: int) -> list[tuple[str, str]]:
        """本次调用要附加的 <system-reminder>（§12），按优先级在预算内取。"""
        out: list[tuple[str, str]] = list(self._attachments)
        warn_at = self._threshold(self._warn_ratio)
        if warn_at and tokens >= warn_at:
            pct = int(tokens * 100 / self._effective)
            out.append((
                "context_usage",
                f"上下文已用约 {pct}%（{tokens}/{self._effective} tokens），即将压缩。请精简输出，"
                "优先完成当前步骤；重要的中间结果写进工作区文件，避免压缩后丢失细节。",
            ))  # fmt: skip
        if (todo := self._todo_reminder(request)) is not None:
            out.append(("todo", todo))
        picked: list[tuple[str, str]] = []
        budget = self._attachment_budget
        for name, text in out:
            cost = estimate_text(text)
            if cost > budget:
                text = text[: int(budget * 2.5)] + "\n…（附件超出预算已截断）"
                cost = budget
            if cost <= 0:
                break
            picked.append((name, text))
            budget -= cost
        return picked

    def _todo_reminder(self, request: Any) -> str | None:
        todos = (request.state or {}).get("todos") if isinstance(request.state, dict) else None
        if not todos:
            return None
        open_items = [t for t in todos if isinstance(t, dict) and t.get("status") != "completed"]
        if not open_items:
            return None
        calls_since = 0
        for m in reversed(list(request.messages)):
            if isinstance(m, AIMessage):
                if any(c.get("name") == "write_todos" for c in m.tool_calls or []):
                    break
                calls_since += 1
        if calls_since < self._todo_after:
            return None
        lines = "\n".join(f"- [{t.get('status')}] {t.get('content')}" for t in open_items[:20])
        return f"待办清单已有 {calls_since} 步没有更新，当前未完成的事项：\n{lines}\n完成或调整后请更新待办。"

    @staticmethod
    def _with_attachments(request: Any, reminders: list[tuple[str, str]]) -> Any:
        if not reminders:
            return request
        body = "\n\n".join(
            f'<system-reminder name="{n}">\n{t}\n</system-reminder>' for n, t in reminders
        )
        note = "（以下为平台附加的上下文，不是用户说的话）\n" + body
        return request.override(messages=[*request.messages, HumanMessage(content=note)])


def _last_ai(resp: Any) -> AIMessage | None:
    if isinstance(resp, AIMessage):
        return resp
    for m in reversed(getattr(resp, "result", None) or []):
        if isinstance(m, AIMessage):
            return m
    return None
