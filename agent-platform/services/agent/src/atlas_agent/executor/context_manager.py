"""原生运行时 · 上下文管理（c4-model/agent/native-runtime/code-native-runtime-context.html）。

为一段交出系统提示词分层（持久指令、记忆常驻块）和上下文流水线（压缩中间件：清理、外置、
压缩、提醒、失败恢复）。摘要和覆盖边界存在 thread_context。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from atlas_engine.kernel.middleware.compaction import CompactionMiddleware
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



@dataclass
class ContextContribution:
    """上下文管理为一段交出的东西。"""

    #: 持久指令（平台 → 用户 → 工作区 ATLAS.md），每轮开始读一次
    instructions: Any
    #: 记忆常驻块。None = 没有记忆
    memory_block: str | None
    #: 上下文流水线（槽位 ④）。None = 智能体关了上下文管理，或模型目录里查不到窗口
    compactor: CompactionMiddleware | None


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
        self, segment: Segment, *, workspace: Any, memory: MemoryTurn | None
    ) -> ContextContribution:
        return ContextContribution(
            # 持久指令（L1）：读不到时跳过，不让本轮失败
            instructions=await load_instructions(
                self._sessionmaker, workspace, segment.thread.created_by_uuid
            ),
            memory_block=memory.block if memory is not None else None,
            compactor=await self._compactor(
                segment,
                workspace=workspace,
                # 记忆的更新提示（§5.5）：每次调模型时现取，Agent 重新加载过的主题不再列出
                dynamic_attachments=memory.reminders if memory is not None else None,
            ),
        )

    async def _compactor(
        self,
        segment: Segment,
        *,
        workspace: Any = None,
        dynamic_attachments: Any = None,
    ) -> CompactionMiddleware | None:
        """按 spec 与模型目录构造上下文管理中间件（doc/context-management-design.html）。

        context_window / max_output_tokens 来自 model_catalog —— 实测落库（§3 D3），
        不能硬编码：opus/sonnet/fable 是 1M，haiku 只有 200k，差 5 倍。
        """
        spec = segment.spec
        cfg = spec.compaction
        if not cfg.enabled:
            return None

        # 经平台时上限来自 LiteLLM 里登记的规格；没登记、没接平台时读 model_catalog 表
        entry = models_of(segment, self._settings).entry(spec.model.model)
        limits = (
            (entry.max_input_tokens, entry.max_output_tokens)
            if entry is not None and entry.max_input_tokens and entry.max_output_tokens
            else None
        )
        if limits is None:
            async with self._sessionmaker() as session:
                limits = await ModelCatalogRepository(session).limits(spec.model.model)
        if limits is None:
            # 目录里没有这个模型 —— 不猜一个窗口值：猜大了压缩永不触发，
            # 猜小了每轮都压。宁可不压，让 token 上限去兜底。
            logger.warning("model_catalog 无 %s 的窗口，跳过压缩", spec.model.model)
            return None
        window, model_max_output = limits
        effective_max_output = min(spec.model.max_output_tokens, model_max_output)

        # 距上次回复多久：超过缓存有效期 → 缓存反正失效，顺手清旧工具结果（§8 ②）
        idle_s = None
        if segment.history_upto is not None:
            idle_s = (datetime.now(UTC) - segment.history_upto).total_seconds()

        return CompactionMiddleware(
            context_window=window,
            max_output_tokens=effective_max_output,
            max_output_cap=model_max_output,
            trigger_ratio=cfg.trigger_ratio,
            clear_ratio=cfg.clear_ratio,
            warn_ratio=cfg.warn_ratio,
            keep_recent_turns=cfg.keep_recent_turns,
            keep_recent_tool_results=cfg.keep_recent_tool_results,
            batch_cap_tokens=self._settings.context_batch_cap_tokens,
            idle_s=idle_s,
            cache_ttl_s=self._settings.prompt_cache_ttl_s,
            filesystem=workspace,
            restore_files=cfg.restore_files,
            dynamic_attachments=dynamic_attachments,
            attachment_budget_tokens=int(
                (window - min(effective_max_output, 20_000)) * cfg.attachment_budget_ratio
            ),
            failure_count=segment.compaction_failures,
            on_failed=make_failure_hook(self._sessionmaker, segment.thread_id),
            summarizer=self._summarizer(segment),
            on_compacted=make_persist_hook(
                self._sessionmaker, segment.thread_id, segment.history_upto
            ),
        )

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
