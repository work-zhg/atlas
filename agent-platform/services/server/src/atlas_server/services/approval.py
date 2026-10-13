"""人工确认的服务端实现（文档 §12.2）。

engine 只认 `ApprovalGate` 协议；这里把它落到 Postgres（审计）+ Redis（唤醒）。

★ 等待从「在线阻塞」改成「挂起」（detail/suspension.html §06）。

  原先 gate 用短 BLPOP 累积到 600s 等决策 —— 一个 asyncio.Task 全程挂着，
  进程重启即丢，而 600s 对小时级任务根本不够。现在 `check()` 立刻返回状态，
  没结果时中间件产出挂起哨兵让本段结束，决策到达后续跑。

  Redis 的决策队列（`run:approval:{id}`）随之整个删掉 —— 它没有消费者了。
  唤醒现在由 HTTP 端点触发（`executor.resume_if_ready`），而决策的权威记录
  始终是 `Approval.status`。留一个没人读的队列只会让下一个人以为有等待方。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.contracts import ApprovalState, Decision
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..repositories.approval import ApprovalRepository
from ..repositories.run import RunRepository
from ..telemetry import semconv as _sc
from ..telemetry import tracer as _tracer

logger = logging.getLogger(__name__)

class RedisApprovalGate:
    """注入给 engine 的审批门禁。

    engine 在工具执行前问它「这次批了吗」；它负责登记、把 run 标成
    awaiting_approval，然后**立刻**返回状态 —— 等待由挂起机制承担。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        run_id: UUID,
    ) -> None:
        """★ 不再有 timeout_s。

        超时判定搬到了恢复扫描（executor 的 sweep_suspended）—— 挂起之后没有
        进程在等，所以「等够了吗」只能由一个周期性的扫描来问，而不是某个
        await 的超时分支。gate 因此不需要知道时限。
        """
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._run_id = run_id

    async def check(
        self, *, approval_id: str, tool_name: str, args: dict[str, Any]
    ) -> ApprovalState:
        """查一次状态。**立刻返回，不阻塞**。

        ★ 首次见到就登记，之后返回真实状态。「是不是首次」靠 approval_id 查库
          判断 —— 而 approval_id 由 (run_id, tool_call_id) 派生，跨段稳定
          （kernel 的 approval_id_for）。这是审批能挂起的前提：续跑时算出同一个
          id，于是查到上次那条记录而不是重新登记一遍。

        ★ 不再有 `_wait()`。原先那段短 BLPOP 累积到 600s 的逻辑整个删掉了 ——
          等待不占进程之后，「等多久」由恢复扫描判定（S8），而不是某个 await
          的超时分支。
        """
        aid = UUID(approval_id)
        try:
            async with self._sessionmaker() as session:
                approvals = ApprovalRepository(session)
                existing = await approvals.get(aid)
                if existing is not None:
                    return existing.status  # type: ignore[return-value]

                await approvals.create(
                    approval_id=aid,
                    run_id=self._run_id,
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=_tool_call_id_of(args),
                )
                # ★ 仍然把 run 标成 awaiting_approval —— 它是 suspended 的一个
                #   细分。UI 要区分「在等机器」和「在等你」：前者用户只能干等，
                #   后者需要他行动（detail/suspension.html §06）。
                await RunRepository(session).set_status(self._run_id, "awaiting_approval")
                await session.commit()
        except Exception:
            # ★ 落库失败**不放行**。高风险工具无人把关地执行是这里最不能接受的
            #   失败方向（§12.2 的红线）—— 这一条没变。
            #
            # ★ 但返回的是 "failed" 而不是 "rejected"。两者的处理相同、含义
            #   相反：一个是系统正常工作的结果，一个是系统没工作。压成同一个值
            #   会让故障在界面上**完全隐形** —— 2026-09-23 一次 tool_name 超
            #   列宽被当成「用户拒绝」喂给 CLI，CLI 停手、子 run 零产出，父模型
            #   只能报「子智能体没有产出文本结论」，根因只在 Pod 日志里。
            #   调用方据此区分文案（contracts/approval.py 的 ApprovalState）。
            logger.exception("审批落库失败，按不放行处理 approval=%s", approval_id)
            return "failed"

        _tracer().start_span(
            "approval_requested",
            attributes={
                _sc.TOOL_NAME: tool_name,
                _sc.RUN_ID: str(self._run_id),
                "atlas.approval.id": approval_id,
            },
        ).end()
        return "pending"


def _tool_call_id_of(args: dict[str, Any]) -> str | None:
    """从工具入参里尽力取出 toolCallId（acp 的 request_permission 带着它）。

    ★ 纯审计用途 —— 取不到就是 None。approval_id 的派生不依赖它（那用的是
      kernel 侧的 tool_call_id），这里只是让排查时少反推一步。
    """
    found = args.get("toolCallId") or args.get("tool_call_id")
    return str(found) if found else None


async def submit_decision(
    session: AsyncSession,
    *,
    approval_id: UUID,
    decision: Decision,
    user_id: UUID,
) -> bool:
    """HTTP 侧写入决策。返回是否成功（重复决策返回 False）。

    ★ **只落库，不唤醒**。唤醒是调用方的事（api/v1/runs.py 在这之后调
      `executor.resume_if_ready`）—— 这里拿不到执行器，而把它注进来只为
      唤醒会让「写决策」和「调度」耦合在一个函数里。

    ★ redis 参数删掉了：那个决策队列没有消费者了（见模块开头）。
    """
    updated = await ApprovalRepository(session).decide(
        approval_id, decision=decision, user_id=user_id
    )
    if not updated:
        return False
    await session.commit()

    # ★ 这里曾经往 Redis 推一条决策消息唤醒阻塞中的 gate（BLPOP）。
    #   审批改成挂起之后那个队列**没有消费者**了 —— 一并删掉，而不是留着。
    #
    #   留着的代价不是那点开销，是误导：下一个人看到 rpush 会以为有等待方，
    #   而真正的唤醒发生在 HTTP 端点里（api/v1/runs.py::decide_approval 调
    #   executor.resume_if_ready）。决策的权威记录始终是 Approval.status。
    return True
