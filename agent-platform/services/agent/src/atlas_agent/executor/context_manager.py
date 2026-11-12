"""原生运行时 · 上下文管理的 server 一侧（native-runtime/code-native-runtime-context.html）。

为一段交出系统提示词分层（持久指令、记忆常驻块），和上下文流水线（槽位 ④）要的端口：窗口
从模型目录查、摘要用哪个模型、摘要和覆盖边界存在 thread_context。流水线本身由 engine 按
spec 拼（atlas_engine/runtime/context.py）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from atlas_engine.contracts.spec import resolve_summarizer_model
from atlas_engine.runtime.context import ContextLimits, ContextPorts, ReminderSource
from atlas_engine.runtime.summarize import Summarizer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.executor.context import load_instructions
from atlas_agent.executor.segment_data import Segment

from ..config import Settings
from ..domain.workspace_layout import WorkspaceLayout
from ..repositories.model_catalog import ModelCatalogRepository
from ..services.compaction import make_failure_hook, make_persist_hook, thread_summarizer
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
    #: 上下文流水线要的端口（engine 按 spec 拼成槽位 ④ 的中间件）
    ports: ContextPorts


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
            ports=ContextPorts(
                offload_store=readable_store,
                result_limit_tokens=self._settings.context_result_limit_tokens,
                batch_cap_tokens=self._settings.context_batch_cap_tokens,
                reminder_sources=sources,
                limits=await self._limits_for(segment, workspace=workspace),
                # ★ 外置的东西放进所用工作区下本会话的隐藏目录：跟着工作区走，各会话分开；
                #   列目录、搜索默认跳过它
                offload_dir=f"{_atlas_dir(segment)}/large_tool_results",
                input_dir=f"{_atlas_dir(segment)}/conversation_history",
            ),
        )

    async def _limits_for(self, segment: Segment, *, workspace: Any) -> ContextLimits | None:
        """压缩要的东西。关了自动压缩、或者模型目录里查不到窗口时为 None。"""
        if not segment.spec.compaction.enabled:
            return None
        limits = await self._limits(segment)
        if limits is None:
            return None
        window, model_max_output = limits
        # 距上次回复已超过缓存有效期 → 缓存反正失效，第一次调模型前顺手清旧工具结果（§8 ②）
        cache_expired = segment.history_upto is not None and (
            (datetime.now(UTC) - segment.history_upto).total_seconds()
            >= self._settings.prompt_cache_ttl_s
        )
        return ContextLimits(
            window=window,
            model_max_output=model_max_output,
            summarizer=self._summarizer(segment),
            workspace=workspace,
            failure_count=segment.compaction_failures,
            on_failed=make_failure_hook(self._sessionmaker, segment.thread_id),
            on_compacted=make_persist_hook(
                self._sessionmaker, segment.thread_id, segment.history_upto
            ),
            cache_expired=cache_expired,
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

    def _summarizer(self, segment: Segment) -> Summarizer:
        """压缩用的摘要器：智能体的推理，和主模型同一个端点（Run 开始时已检查过）。"""
        spec = segment.spec
        name = resolve_summarizer_model(spec.compaction.summarizer_model, spec.model.model)
        return thread_summarizer(
            self._settings,
            self._build_model,
            model=name,
            endpoint=models_of(segment, self._settings).endpoint,
            provider=spec.model.provider,
        )


def _atlas_dir(segment: Segment) -> str:
    """本会话的隐藏目录在沙箱里的路径（/data/files/{工作目录}/.atlas/{会话}）。"""
    t = segment.thread
    layout = WorkspaceLayout.of(t.created_by_uuid, t.thread_uuid, t.workspace_dir)
    return layout.real(layout.atlas_dir)
