"""AgentRuntime —— 「一轮怎么跑」的接缝（acp 详设 §02）。

执行器的链路里，前后两段与 agent 类型无关：

    load_for_execution → _prepare → mark_running     ← 无关
    ┌───────────────────────────────────────────┐
    │ 装配 → 驱动 → 产出 TraceEvent 流            │  ← 只有这一段有类型之分
    └───────────────────────────────────────────┘
    收事件 → persist → archive → 释放会话锁         ← 无关

把中间那段抽成协议，acp 就只是第二个实现 —— 子会话、会话串行锁、幂等、
SSE 回放、孤儿回收全部原样生效，因为它们都挂在 **run 的生命周期**上，
不挂在执行方式上。

★ 这是 server 内部的缝，不进 contracts：两端（executor 与各 runtime）都是
  server 的东西，kernel 与 bridge 都不认识它。

★ 本模块**不在纯计算层**（与 build.py / runner.py 不同）：它经
  RedisCancelToken 拖进 redis。这条边界是对的 —— build 管「图长什么样」、
  runner 管「怎么跑完一轮」，两者都能脱离基础设施单测；runtime 正是把
  它们接到基础设施上的那一层，本来就该认识 redis。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from atlas_engine.contracts import EngineError, InvalidSpec

from atlas_server.executor.runner import failed_before_start
from atlas_server.executor.runner import run as engine_run

from ..domain.events import EventType
from ..errors import CapabilityUnavailable
from ..stream.relay import RedisCancelToken

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..domain.events import TraceEvent
    from ..domain.messages import Transcript
    from ..stream.relay import EventRelay
    from .assembly import HookAssembly, PreparedRun

__all__ = ["AgentRuntime", "NativeRuntime", "select_runtime"]


logger = logging.getLogger(__name__)


@runtime_checkable
class AgentRuntime(Protocol):
    """在一个已就绪的 run 上执行一轮，产出事件流直到终态。

    ★ 实现方**不落库、不发布、不管锁** —— 那三件事在执行器的前后段，
      两类 run 共用。这里只负责「把一轮跑完并把过程讲出来」。

    ★ 也不做重试：失败是 run.failed 事件，重试语义归用户（他们看得到
      retryable 标记）。在这里偷偷重试会让用量翻倍且无从解释。
    """

    def run_turn(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        transcript: Transcript | None = None,
    ) -> AsyncIterator[TraceEvent]:
        """redis / relay 是**每 run 新建**的（后台任务不能用请求级连接）。

        两类 runtime 都要它们：relay 出取消令牌，redis 供审批门禁 /
        acp 的通道复用。

        transcript: 执行器用来收本轮实际产生的消息（模型的 tool_use、工具的
            结果），落库后下一段执行才能重建出完整历史。

            ★ 这不违反「runtime 不落库」—— 它只往 sink 里放对象，写库仍然
              发生在执行器的后段。做成出参而不是从事件流重建，是因为事件流
              是给人看的投影：正文的分段边界靠 `Answer.seal()` 的时机反推，
              模型连着调两批工具、中间没说话时顺序就再也对不上了。

            ★ acp 实现**不填它**。CLI 的工具调用发生在 Pod 内部，平台侧只看到
              update 流；它的历史恢复走 `external_session_id` + session/load，
              根本不经过 message 表（acp 详设 §09）。
        """
        ...


class NativeRuntime:
    """进程内跑 LangGraph 图 —— 现状的执行方式，原样收编。

    `assembly` 是进程级的（它持有沙箱/编码后端之类的实例态），所以构造一次
    复用；每轮变化的东西全在 run_turn 的参数里。
    """

    def __init__(self, assembly: HookAssembly) -> None:
        self._assembly = assembly

    async def run_turn(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        transcript: Transcript | None = None,
    ) -> AsyncIterator[TraceEvent]:
        # 图在 server 侧装配（build.py 是策略，assembly 收集 IO 能力），
        # runner 只驱动已装好的图 —— cancel / titler 是 runner 自己的注入点。
        notices: list[tuple[EventType, dict[str, Any]]] = []
        try:
            graph, titler = await self._assembly.build(
                prepared, run_id=run_id, redis=redis, relay=relay, notices=notices
            )
        except Exception as exc:
            # ★ 装配失败也要有 run.started → run.failed，否则 SSE 上一个事件都
            #   没有，用户只能干等（见 failed_before_start）。CancelledError 是
            #   BaseException，不在这里 —— 取消照常穿透。
            kind, message, details = _classify_assembly_error(exc)
            for event in failed_before_start(
                prepared.spec,
                run_id=run_id,
                kind=kind,
                message=message,
                details=details,
                start_seq=prepared.start_seq,
                base_depth=prepared.base_depth,
            ):
                yield event
            return
        async for event in engine_run(
            prepared.spec,
            run_id=run_id,
            graph=graph,
            input_content=prepared.input_content,
            history=prepared.history,
            cancel=RedisCancelToken(relay, run_id),
            titler=titler,
            transcript=transcript,
            start_seq=prepared.start_seq,
            resume=prepared.resume,
            prior_tokens=prepared.prior_tokens,
            base_depth=prepared.base_depth,
            notices=notices,
        ):
            yield event


def _classify_assembly_error(exc: Exception) -> tuple[str, str, dict[str, object]]:
    """装配异常 → (error_kind, 给用户看的消息, 附加字段)。

    ★ 只有两类异常的消息原样给用户：CapabilityUnavailable（约定面向用户）与
      EngineError（契约内的错误，如 InvalidSpec）。其余是 bug —— 细节只进
      日志，用户看到的仍是「执行器内部错误」，与此前的兜底文案一致。
    """
    if isinstance(exc, EngineError):
        logger.warning("run 装配失败：%s", exc.message)
        return exc.kind, exc.message, dict(exc.details)
    if isinstance(exc, CapabilityUnavailable):
        logger.warning("run 装配失败：%s", exc)
        return CapabilityUnavailable.kind, str(exc), {}
    logger.exception("run 装配时发生未预期的异常")
    return "internal_error", "执行器内部错误", {}


def select_runtime(
    prepared: PreparedRun, *, native: NativeRuntime, acp: AgentRuntime | None = None
) -> AgentRuntime:
    """按 agent 类型选执行方式。

    刻意做成函数而不是 if 散在执行器里：选择的依据（spec）与可选项（runtime
    实例）都摆在签名上，加一种类型时一眼看得出要补什么。

    ★ kind="acp" 但没注入 HostRuntime 时**报错**，不静默回落 native ——
      回落的后果是「配了 CLI 助理，实际跑的是进程内 LangGraph」，两者的
      工具面与上下文语义完全不同，而事件流上看不出区别（§13.2）。
    """
    if prepared.spec.kind == "acp":
        if acp is None:
            msg = "agent 的 kind='acp' 但未注入 HostRuntime（需配置 acp 执行环境）"
            raise InvalidSpec(msg, agent=prepared.spec.slug)
        return acp
    return native
