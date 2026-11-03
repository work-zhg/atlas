"""原生运行时 · 上下文管理（c4-model/agent/native-runtime/code-native-runtime-context.html）。

为一段交出系统提示词分层（持久指令、记忆常驻块）和上下文流水线（槽位 ④：外置、清理、
压缩、提醒、失败恢复）。摘要和覆盖边界存在 thread_context。

★ 智能体关了自动压缩、或者模型目录里查不到窗口时，流水线只剩外置和提醒 —— 它们不是压缩，
  关压缩不该把大结果保护和记忆提醒一起关掉。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from atlas_engine.kernel.context import (
    CallRecovery,
    Compactor,
    ContextBudget,
    ContextMiddleware,
    Reminders,
    ReminderSource,
    ResultClearing,
    ResultOffloader,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.spec import resolve_summarizer_model
from atlas_agent.executor.context import load_instructions
from atlas_agent.executor.segment_data import Segment

from ..config import Settings
from ..repositories.model_catalog import ModelCatalogRepository
from ..services.compaction import ThreadSummarizer, make_failure_hook, make_persist_hook
from .run_models import ModelBuilder, models_of

if TYPE_CHECKING:
    from ..memory import MemoryTurn

logger = logging.getLogger(__name__)

#: 不知道窗口时提醒的总量上限（知道时按 attachment_budget_ratio 算）
_REMINDER_BUDGET_WITHOUT_WINDOW = 4_000


@dataclass
class ContextContribution:
    """上下文管理为一段交出的东西。"""

    #: 持久指令（平台 → 用户 → 工作区 ATLAS.md），每轮开始读一次
    instructions: Any
    #: 记忆常驻块。None = 没有记忆
    memory_block: str | None
    #: 上下文流水线（槽位 ④）
    middleware: ContextMiddleware


class ContextManager:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        model_builder: ModelBuilder,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._build_model = model_builder

    async def contribute(
        self,
        segment: Segment,
        *,
        workspace: Any,
        memory: MemoryTurn | None,
        reminders: Sequence[ReminderSource] = (),
        readable_store: Any = None,
    ) -> ContextContribution:
        """reminders：别的领域给的提醒来源（工具面的待办）；readable_store：模型能用 read_file
        读回来的存储（工具面给，没有文件工具时为 None）—— 大结果外置写进这里。"""
        sources: list[ReminderSource] = []
        if memory is not None:
            # 记忆的更新提示（§5.5）：每次调模型时现取，Agent 重新加载过的主题不再列出
            turn = memory
            sources.append(lambda _state: turn.reminders())
        sources.extend(reminders)
        return ContextContribution(
            # 持久指令（L1）：读不到时跳过，不让本轮失败
            instructions=await load_instructions(
                self._sessionmaker, workspace, segment.thread.created_by_uuid
            ),
            memory_block=memory.block if memory is not None else None,
            middleware=await self._pipeline(
                segment, workspace=workspace, readable_store=readable_store, sources=sources
            ),
        )

    async def _pipeline(
        self,
        segment: Segment,
        *,
        workspace: Any,
        readable_store: Any,
        sources: Sequence[ReminderSource],
    ) -> ContextMiddleware:
        """按 spec 与模型目录构造流水线（doc/context-management-design.html）。"""
        offloader = ResultOffloader(
            store=readable_store,
            single_limit_tokens=self._settings.context_result_limit_tokens,
            batch_cap_tokens=self._settings.context_batch_cap_tokens,
        )
        cfg = segment.spec.compaction
        limits = await self._limits(segment) if cfg.enabled else None
        if limits is None:
            return ContextMiddleware(
                offloader=offloader,
                reminders=Reminders(budget_tokens=_REMINDER_BUDGET_WITHOUT_WINDOW, sources=sources),
            )
        window, model_max_output = limits
        budget = ContextBudget(
            context_window=window,
            max_output_tokens=min(segment.spec.model.max_output_tokens, model_max_output),
            compact_ratio=cfg.trigger_ratio,
            clear_ratio=cfg.clear_ratio,
            warn_ratio=cfg.warn_ratio,
        )
        compactor = Compactor(
            summarizer=self._summarizer(segment),
            keep_recent_turns=cfg.keep_recent_turns,
            workspace=workspace,
            restore_files=cfg.restore_files,
            failure_count=segment.compaction_failures,
            on_failed=make_failure_hook(self._sessionmaker, segment.thread_id),
            on_compacted=make_persist_hook(
                self._sessionmaker, segment.thread_id, segment.history_upto
            ),
        )
        # 距上次回复已超过缓存有效期 → 缓存反正失效，第一次调模型前顺手清旧工具结果（§8 ②）
        cache_expired = segment.history_upto is not None and (
            (datetime.now(UTC) - segment.history_upto).total_seconds()
            >= self._settings.prompt_cache_ttl_s
        )
        return ContextMiddleware(
            offloader=offloader,
            reminders=Reminders(
                budget_tokens=int(budget.effective * cfg.attachment_budget_ratio), sources=sources
            ),
            budget=budget,
            clearing=ResultClearing(
                keep_recent=cfg.keep_recent_tool_results, cache_expired=cache_expired
            ),
            compactor=compactor,
            recovery=CallRecovery(compactor=compactor, max_output_cap=model_max_output),
        )

    async def _limits(self, segment: Segment) -> tuple[int, int] | None:
        """(窗口, 模型的最大输出)。来自 model_catalog —— 实测落库（§3 D3），不能硬编码：
        opus/sonnet/fable 是 1M，haiku 只有 200k，差 5 倍。"""
        model = segment.spec.model.model
        # 经平台时上限来自 LiteLLM 里登记的规格；没登记、没接平台时读 model_catalog 表
        entry = models_of(segment, self._settings).entry(model)
        if entry is not None and entry.max_input_tokens and entry.max_output_tokens:
            return entry.max_input_tokens, entry.max_output_tokens
        async with self._sessionmaker() as session:
            limits = await ModelCatalogRepository(session).limits(model)
        if limits is None:
            # 目录里没有这个模型 —— 不猜一个窗口值：猜大了压缩永不触发，
            # 猜小了每轮都压。宁可不压，让 token 上限去兜底。
            logger.warning("model_catalog 无 %s 的窗口，不按档位清理和压缩", model)
        return limits

    def _summarizer(self, segment: Segment) -> ThreadSummarizer:
        """压缩用的摘要器：智能体的推理，和主模型同一个端点（Run 开始时已检查过）。"""
        spec = segment.spec
        name = resolve_summarizer_model(spec.compaction.summarizer_model, spec.model.model)
        return ThreadSummarizer(
            self._settings,
            self._build_model,
            model=name,
            endpoint=models_of(segment, self._settings).endpoint,
            provider=spec.model.provider,
        )
