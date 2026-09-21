"""人工确认的服务端实现（文档 §12.2）。

engine 只认 `ApprovalGate` 协议；这里把它落到 Postgres（审计）+ Redis（唤醒）。

**用 BLPOP 而不是 pub/sub**：
  · pub/sub 要求订阅先于发布，否则消息丢失 —— 而 HTTP 决策什么时候到
    完全不可控，"用户在我们订阅前就点了批准" 是真实会发生的
  · BLPOP 读的是队列：决策先到就已经躺在队列里，等待方立刻取走
  · 顺带自带超时语义，不必自己写 wait_for
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.contracts import Decision
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..repositories.approval import ApprovalRepository
from ..repositories.run import RunRepository
from ..telemetry import semconv as _sc
from ..telemetry import tracer as _tracer

logger = logging.getLogger(__name__)

#: §12.2：超过此时长未决策视为 expired，等同拒绝
DEFAULT_TIMEOUT_S = 600

#: 单次 BLPOP 的阻塞时长。短一些，连接不被长期独占
_POLL_SLICE_S = 5


def decision_key(approval_id: UUID | str) -> str:
    return f"run:approval:{approval_id}"


class RedisApprovalGate:
    """注入给 engine 的审批门禁。

    engine 在工具执行前 await 它；它负责落库、把 run 标成 awaiting_approval，
    然后阻塞等待 HTTP 侧的决策。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        run_id: UUID,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._run_id = run_id
        self._timeout_s = timeout_s

    async def request(self, *, approval_id: str, tool_name: str, args: dict[str, Any]) -> Decision:
        aid = UUID(approval_id)
        try:
            async with self._sessionmaker() as session:
                await ApprovalRepository(session).create(
                    approval_id=aid, run_id=self._run_id, tool_name=tool_name, args=args
                )
                await RunRepository(session).set_status(self._run_id, "awaiting_approval")
                await session.commit()
        except Exception:
            # 落库失败不该让工具卡死。放行是更差的选择（高风险工具无人把关），
            # 所以按拒绝处理，agent 会收到提示并换方案。
            logger.exception("审批落库失败，按拒绝处理 approval=%s", approval_id)
            return "rejected"

        # ★ 「等人点头」必须是**独立 span**（可观测性设计 §04）。
        #
        #   一次 run 里可能有好几分钟花在等用户点「允许」上。混在 Run 总耗时
        #   里的话，所有延迟统计都会被它污染 —— P95 高得离谱，却分不清是系统
        #   慢还是用户在吃午饭。前者是工程问题，后者是产品问题，处置方式完全
        #   不同，所以指标上也要能分开。
        #
        #   实测过一次：委派的子智能体等审批等满 300s 直到 adapter 超时。
        #   那种情况下，看到「等待 span 占了 300s」与看到「Run 耗时 300s」
        #   是两种完全不同的排查起点。
        with _tracer().start_as_current_span(
            "await_approval",
            attributes={
                _sc.TOOL_NAME: tool_name,
                _sc.RUN_ID: str(self._run_id),
                "atlas.approval.id": approval_id,
            },
        ) as span:
            decision = await self._wait(aid)
            span.set_attribute("atlas.approval.decision", decision)

        try:
            async with self._sessionmaker() as session:
                if decision == "expired":
                    await ApprovalRepository(session).expire(aid)
                await RunRepository(session).set_status(self._run_id, "running")
                await session.commit()
        except Exception:
            logger.warning("审批后状态回写失败 approval=%s", approval_id, exc_info=True)

        return decision

    async def _wait(self, approval_id: UUID) -> Decision:
        """短 BLPOP 累积到总时限。

        ★ 不用一次 `BLPOP timeout=600`：那会把一条 Redis 连接独占十分钟，
          撞上客户端 socket 读超时，且期间连接断了就再也醒不过来。
          分成若干次短阻塞，每次都是完整的一轮请求-响应，断连由重试自愈。
        """
        key = decision_key(approval_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s

        while loop.time() < deadline:
            slice_s = max(1, min(_POLL_SLICE_S, int(deadline - loop.time())))
            try:
                item = await self._redis.blpop([key], timeout=slice_s)
            except Exception:
                logger.warning("等待审批时 Redis 出错，继续重试", exc_info=True)
                await asyncio.sleep(1)
                continue

            if item is not None:
                return "approved" if item[1] == "approved" else "rejected"

        return "expired"


async def submit_decision(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    approval_id: UUID,
    decision: Decision,
    user_id: UUID,
) -> bool:
    """HTTP 侧写入决策并唤醒等待中的执行器。返回是否成功（重复决策返回 False）。"""
    updated = await ApprovalRepository(session).decide(
        approval_id, decision=decision, user_id=user_id
    )
    if not updated:
        return False
    await session.commit()

    # 先落库再唤醒：反过来的话执行器可能在决策入库前就继续跑，
    # 审计表里会留下一条永远 pending 的记录。
    key = decision_key(approval_id)
    await redis.rpush(key, decision)
    # 等待方若已超时离开，这条消息没人取 —— 加 TTL 防止 key 永久残留
    await redis.expire(key, DEFAULT_TIMEOUT_S)
    return True
