"""上下文流水线的装配（槽位 ④，doc/context-management-design.html）：按 spec 和端口拼出来。

流水线的各个阶段在 kernel/context（预算、旧结果清理、大结果外置、压缩、提醒、失败恢复），
它们只认标量和回调；这里按智能体配置把它们拼起来。窗口从模型目录查、摘要存哪、提醒从哪来，
都由 server 交进来（ContextPorts）—— 这里不碰 DB。

★ 智能体关了自动压缩、或者不知道窗口时，流水线只剩外置、图片上限和提醒 —— 它们不是压缩，关压缩
  不该把大结果保护、图片上限和记忆提醒一起关掉。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from atlas_engine.contracts.spec import AgentSpec
from atlas_engine.kernel.context import (
    DEFAULT_CLEARABLE_TOOLS,
    CallRecovery,
    Compactor,
    ContextBudget,
    ContextMiddleware,
    ImageLimit,
    Reminders,
    ReminderSource,
    ResultClearing,
    ResultOffloader,
)
from atlas_engine.kernel.context.offload import INPUT_DIR, OFFLOAD_DIR

__all__ = ["ContextLimits", "ContextPorts", "ReminderSource", "context_pipeline"]

#: 不知道窗口时提醒的总量上限（知道时按 attachment_budget_ratio 算）
_REMINDER_BUDGET_WITHOUT_WINDOW = 4_000

#: 主上下文里最多留几张工具结果里的图（kernel/context/images.py）。0 = 看一次就走：MiniMax 每次
#: 请求都把上下文里的图重新处理一遍（2 张首字中位约 8 秒），留着就每步都慢；要对比前后，靠模型
#: 看图时写下的要点或再读一次。★ 改这个值要一起改 IMAGE_SEEN_ONCE_HINT 的措辞
_KEEP_IMAGES = 0


@dataclass(frozen=True)
class ContextLimits:
    """压缩要的东西：开了自动压缩才有。"""

    #: 模型的上下文窗口、最大输出（模型平台登记的值；没登记时 server 按保守值兜底）
    window: int
    model_max_output: int
    #: 摘要器（runtime/summarize.py 的 Summarizer）
    summarizer: Any
    #: 压缩后恢复最近读过的文件用的工作区
    workspace: Any = None
    #: 自动压缩已经连续失败了几次（熔断用）
    failure_count: int = 0
    on_failed: Callable[[int], Awaitable[None]] | None = None
    #: 压缩成功：把摘要和覆盖边界存下来（下一轮从这里切历史）
    on_compacted: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    #: 距上次回复已超过提示词缓存的有效期：缓存反正失效，第一次调模型前顺手清旧工具结果
    cache_expired: bool = False
    #: 窗口是兜底的估计值（模型没登记窗口）：context.usage 里带上，用量条据此提示
    window_assumed: bool = False


@dataclass(frozen=True)
class ContextPorts:
    """上下文流水线要从调用方拿的东西。"""

    #: 模型能用 read_file 读回来的存储，大结果外置写进这里。None = 只能截断
    offload_store: Any
    #: 单个工具结果、一批工具结果的外置阈值（token）
    result_limit_tokens: int
    batch_cap_tokens: int
    #: 每次调模型前追加的提醒来自哪里（记忆的更新、待办）
    reminder_sources: Sequence[ReminderSource] = field(default_factory=tuple)
    #: None = 关了自动压缩
    limits: ContextLimits | None = None
    #: 外置的工具结果、超长用户输入写进 offload_store 的哪个目录（模型看到的就是这里的路径）
    offload_dir: str = OFFLOAD_DIR
    input_dir: str = INPUT_DIR


def context_pipeline(
    spec: AgentSpec, ports: ContextPorts, *, regenerable: Iterable[str] = ()
) -> ContextMiddleware:
    """regenerable：默认清单之外、结果也可以清的工具（只读的外部工具，见 tool_registry）。"""
    offloader = ResultOffloader(
        store=ports.offload_store,
        single_limit_tokens=ports.result_limit_tokens,
        batch_cap_tokens=ports.batch_cap_tokens,
        offload_dir=ports.offload_dir,
        input_dir=ports.input_dir,
    )
    sources = list(ports.reminder_sources)
    limits = ports.limits
    if limits is None:
        return ContextMiddleware(
            offloader=offloader,
            reminders=Reminders(budget_tokens=_REMINDER_BUDGET_WITHOUT_WINDOW, sources=sources),
            images=ImageLimit(keep=_KEEP_IMAGES),
        )
    cfg = spec.compaction
    budget = ContextBudget(
        context_window=limits.window,
        max_output_tokens=min(spec.model.max_output_tokens, limits.model_max_output),
        compact_ratio=cfg.trigger_ratio,
        clear_ratio=cfg.clear_ratio,
        warn_ratio=cfg.warn_ratio,
        window_assumed=limits.window_assumed,
    )
    compactor = Compactor(
        summarizer=limits.summarizer,
        keep_recent_turns=cfg.keep_recent_turns,
        workspace=limits.workspace,
        restore_files=cfg.restore_files,
        failure_count=limits.failure_count,
        on_failed=limits.on_failed,
        on_compacted=limits.on_compacted,
        offload_dir=ports.offload_dir,
    )
    return ContextMiddleware(
        offloader=offloader,
        reminders=Reminders(
            budget_tokens=int(budget.effective * cfg.attachment_budget_ratio), sources=sources
        ),
        budget=budget,
        # ★ 只读的外部工具（MCP 的搜索之类）也清：不清的话一次搜索 2 万 token 的原始结果一直
        #   留在上下文里（会话 01a1f830：3 次搜索占了 171k 里的三分之一，跟了 280 次调用）
        clearing=ResultClearing(
            keep_recent=cfg.keep_recent_tool_results,
            clearable=DEFAULT_CLEARABLE_TOOLS | frozenset(regenerable),
            cache_expired=limits.cache_expired,
        ),
        compactor=compactor,
        recovery=CallRecovery(compactor=compactor, max_output_cap=limits.model_max_output),
        images=ImageLimit(keep=_KEEP_IMAGES),
    )
