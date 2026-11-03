"""上下文管理的流水线（c4-model/agent/native-runtime/code-native-runtime-context.html）。

ContextMiddleware 只做接线（槽位 ④），每个阶段一个策略：预算（ContextBudget）、旧结果清理
（ResultClearing）、大结果外置（ResultOffloader）、压缩（Compactor）、提醒（Reminders）、
失败恢复（CallRecovery）。策略都只认标量和回调，不认识 AgentSpec —— 由 server 按智能体配置构造。
"""

from atlas_engine.kernel.context._text import SUMMARY_PREFIX, estimate_text, estimate_tokens
from atlas_engine.kernel.context.budget import ContextBudget, Level
from atlas_engine.kernel.context.clearing import DEFAULT_CLEARABLE_TOOLS, ResultClearing
from atlas_engine.kernel.context.compactor import DEGRADED_SUMMARY, Compactor, Summarizer
from atlas_engine.kernel.context.middleware import ContextMiddleware
from atlas_engine.kernel.context.offload import ResultOffloader
from atlas_engine.kernel.context.recovery import CallRecovery
from atlas_engine.kernel.context.reminders import Reminders, ReminderSource

__all__ = [
    "DEFAULT_CLEARABLE_TOOLS",
    "DEGRADED_SUMMARY",
    "SUMMARY_PREFIX",
    "CallRecovery",
    "Compactor",
    "ContextBudget",
    "ContextMiddleware",
    "Level",
    "ReminderSource",
    "Reminders",
    "ResultClearing",
    "ResultOffloader",
    "Summarizer",
    "estimate_text",
    "estimate_tokens",
]
