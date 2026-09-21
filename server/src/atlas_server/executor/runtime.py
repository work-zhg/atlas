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

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from atlas_engine.contracts import InvalidSpec

from atlas_server.executor.runner import run as engine_run

from ..stream.relay import RedisCancelToken

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..domain.events import TraceEvent
    from ..stream.relay import EventRelay
    from .assembly import HookAssembly, PreparedRun

__all__ = ["AgentRuntime", "NativeRuntime", "select_runtime"]


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
    ) -> AsyncIterator[TraceEvent]:
        """redis / relay 是**每 run 新建**的（后台任务不能用请求级连接）。

        两类 runtime 都要它们：relay 出取消令牌，redis 供审批门禁 /
        acp 的通道复用。
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
    ) -> AsyncIterator[TraceEvent]:
        # 图在 server 侧装配（build.py 是策略，assembly 收集 IO 能力），
        # runner 只驱动已装好的图 —— cancel / titler 是 runner 自己的注入点。
        graph, titler = await self._assembly.build(
            prepared, run_id=run_id, redis=redis, relay=relay
        )
        async for event in engine_run(
            prepared.spec,
            run_id=run_id,
            graph=graph,
            input_content=prepared.input_content,
            history=prepared.history,
            cancel=RedisCancelToken(relay, run_id),
            titler=titler,
        ):
            yield event


def select_runtime(
    prepared: PreparedRun, *, native: NativeRuntime, acp: AgentRuntime | None = None
) -> AgentRuntime:
    """按 agent 类型选执行方式。

    刻意做成函数而不是 if 散在执行器里：选择的依据（spec）与可选项（runtime
    实例）都摆在签名上，加一种类型时一眼看得出要补什么。

    ★ kind="acp" 但没注入 AcpRuntime 时**报错**，不静默回落 native ——
      回落的后果是「配了 CLI 助理，实际跑的是进程内 LangGraph」，两者的
      工具面与上下文语义完全不同，而事件流上看不出区别（§13.2）。
    """
    if prepared.spec.kind == "acp":
        if acp is None:
            msg = "agent 的 kind='acp' 但未注入 AcpRuntime（需配置 acp 执行环境）"
            raise InvalidSpec(msg, agent=prepared.spec.slug)
        return acp
    return native
