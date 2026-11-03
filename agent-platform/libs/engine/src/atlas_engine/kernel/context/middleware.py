"""上下文流水线的接线（c4-model/agent/native-runtime/code-native-runtime-context.html，槽位 ④）。

自己不做判断，每个阶段交给一个策略：

    abefore_model（模型调用前，改 state —— 本段后续调用都看到）
      ⓪ 应用上一次反应式压缩的结果
      ① 外置：超长的用户输入；最后一批工具结果总量封顶（ResultOffloader）
      ② 旧工具结果清理：到了清理档，或距上次回复已超过缓存有效期（ResultClearing）
      ③ 自动压缩：到了压缩档（Compactor，连续失败 N 次熔断）

    awrap_model_call（这一次调用，不改 state）
      ④ 提醒：<system-reminder> 追加在最后，不落库、不进历史（Reminders）
      ⑤ 失败恢复：上下文过长 → 强制压缩后重试；输出被截断 → 调高输出上限重试（CallRecovery）
      ⑥ 构成观测：context.usage 事件（各层 token、实际 usage、档位）

    awrap_tool_call（每次工具调用后）
      ⑦ 单条大结果外置（ResultOffloader）

按档位的阶段（②③、用量提醒、⑥）要有预算（ContextBudget）；智能体关了自动压缩、或者不知道
模型的窗口时不给预算，流水线只剩外置和提醒 —— 它们不是压缩，关压缩不该把大结果保护和记忆
提醒一起关掉。

★ 所有改写只作用于模型看到的 state；消息表不受影响。
★ 压缩写回 state 用「清空 + 按序重写」：只删压缩区再追加摘要的话，reducer 把摘要追加在**末尾**，
  模型看到的是 [最近消息…, 摘要] —— 最后一条是 assistant，下一次调用就成了对摘要的续写。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from atlas_engine.kernel.context._text import emit, estimate_text, estimate_tokens, plain_text
from atlas_engine.kernel.context.budget import ContextBudget, Level, composition
from atlas_engine.kernel.context.clearing import ResultClearing
from atlas_engine.kernel.context.compactor import Compactor
from atlas_engine.kernel.context.offload import ResultOffloader
from atlas_engine.kernel.context.recovery import CallRecovery
from atlas_engine.kernel.context.reminders import Reminders

logger = logging.getLogger(__name__)

__all__ = ["ContextMiddleware"]

#: 没有预算（不知道窗口）时提醒的总量上限
_REMINDER_BUDGET_WITHOUT_WINDOW = 4_000


class ContextMiddleware(AgentMiddleware):
    """上下文流水线。策略都可缺省：None = 这个阶段不做。"""

    def __init__(
        self,
        *,
        offloader: ResultOffloader | None = None,
        reminders: Reminders | None = None,
        budget: ContextBudget | None = None,
        clearing: ResultClearing | None = None,
        compactor: Compactor | None = None,
        recovery: CallRecovery | None = None,
    ) -> None:
        super().__init__()
        if budget is None and (clearing is not None or compactor is not None):
            raise ValueError("清理和压缩按档位触发，必须同时给 ContextBudget")
        self._offloader = offloader
        self._budget = budget
        self._clearing = clearing
        self._compactor = compactor
        self._recovery = recovery
        self._reminders = reminders or Reminders(
            budget_tokens=max(1_000, budget.effective // 10)
            if budget is not None
            else _REMINDER_BUDGET_WITHOUT_WINDOW
        )
        #: 反应式压缩的结果：(压缩后的消息, 压缩时 state 的长度)，下一个 before_model 写回 state
        self._pending: tuple[list[BaseMessage], int] | None = None

    def _tokens(self, messages: Sequence[BaseMessage]) -> int:
        return self._budget.tokens(messages) if self._budget is not None else estimate_tokens(messages)

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

        def apply(updates: Sequence[BaseMessage]) -> None:
            # 按 id 原地替换（reducer 同样按 id 合并）；没有 id 的无从替换
            nonlocal messages
            replaced.update({str(m.id): m for m in updates if m.id})
            messages = [replaced.get(str(m.id), m) for m in messages]

        # ① 外置
        if self._offloader is not None:
            if (offloaded := await self._offloader.on_input(messages)) is not None:
                apply([offloaded])
            apply(await self._offloader.on_batch(messages))

        if self._budget is not None:
            tokens = self._budget.tokens(messages)

            # ② 旧工具结果清理
            if self._clearing is not None and (
                reason := self._clearing.due(at_clear_level=self._budget.reached(Level.CLEAR, tokens))
            ):
                plan = self._clearing.plan(messages)
                if plan:
                    ids = {str(p.id) for p in plan}
                    freed = sum(estimate_text(plain_text(m)) for m in messages if str(m.id) in ids)
                    freed -= sum(estimate_text(plain_text(p)) for p in plan)
                    apply(plan)
                    tokens = max(0, tokens - freed)
                    emit({"kind": "context.cleared", "cleared": len(plan), "tokens_freed": freed, "reason": reason})

            # ③ 自动压缩
            if (
                self._compactor is not None
                and not self._compactor.circuit_open
                and self._budget.reached(Level.COMPACT, tokens)
            ):
                messages = await self._compactor.compact(
                    messages, tokens=tokens, reason="threshold", measure=self._tokens
                )
                rewrite = True

        if rewrite:
            return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]}
        if replaced:
            return {"messages": list(replaced.values())}
        return None

    # ------------------------------------------------------------------ wrap_model_call

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        messages = list(request.messages)
        budget = self._budget
        comp = None
        usage_note: list[tuple[str, str]] = []
        if budget is not None:
            system_text = plain_text(request.system_message) if request.system_message is not None else ""
            comp = composition(system_text=system_text, tools=list(request.tools or []),
                               messages=messages, attachment_tokens=0)  # fmt: skip
            budget.observe_overhead(comp.system + comp.tools)
            tokens, basis = budget.measure(messages)
            if budget.reached(Level.WARN, tokens):
                pct = int(tokens * 100 / budget.effective)
                usage_note.append((
                    "context_usage",
                    f"上下文已用约 {pct}%（{tokens}/{budget.effective} tokens），即将压缩。请精简输出，"
                    "优先完成当前步骤；重要的中间结果写进工作区文件，避免压缩后丢失细节。",
                ))  # fmt: skip
        else:
            tokens, basis = estimate_tokens(messages), "estimate"

        # ④ 提醒
        reminders = self._reminders.collect(request.state or {}, extra=usage_note)
        req = Reminders.attach(request, reminders)

        # ⑤ 失败恢复
        try:
            resp = await handler(req)
        except Exception as exc:
            if self._recovery is None or self._pending is not None:
                raise
            compacted = await self._recovery.shrink(exc, messages, tokens=tokens, measure=self._tokens)
            if compacted is None:
                raise
            logger.warning("模型返回上下文过长，强制压缩后重试：%s", str(exc)[:200])
            self._pending = (compacted, len(messages))
            req = Reminders.attach(request.override(messages=compacted), reminders)
            resp = await handler(req)

        ai = _last_ai(resp)
        if self._recovery is not None and (settings := self._recovery.raise_output(request, ai)):
            logger.info("输出被截断，max_tokens 提高到 %s 重试一次", settings["max_tokens"])
            resp = await handler(req.override(model_settings=settings))
            ai = _last_ai(resp)

        # ⑥ 构成观测
        if budget is not None and comp is not None:
            usage = (getattr(ai, "usage_metadata", None) or {}) if ai is not None else {}
            comp.attachments = sum(estimate_text(t) for _, t in reminders)
            emit({
                "kind": "context.usage",
                "tokens": int(usage.get("input_tokens") or 0) or tokens + comp.attachments,
                "basis": "usage" if usage.get("input_tokens") else basis,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "context_window": budget.window,
                "effective_window": budget.effective,
                "thresholds": budget.thresholds(),
                "composition": comp.as_dict(),
                "attachments": [n for n, _ in reminders],
                "compaction_disabled": self._compactor is None or self._compactor.circuit_open,
            })  # fmt: skip
        return resp

    # ------------------------------------------------------------------ wrap_tool_call

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        # ⑦ 单条大结果外置
        result = await handler(request)
        if self._offloader is None:
            return result
        return await self._offloader.on_result(result, tool_name=str(request.tool_call.get("name") or ""))


def _last_ai(resp: Any) -> AIMessage | None:
    if isinstance(resp, AIMessage):
        return resp
    for m in reversed(list(getattr(resp, "result", None) or [])):
        if isinstance(m, AIMessage):
            return m
    return None
