"""InstructionDispatcher：提交后把排着的指令投给 Agent 平台、把要取消的 run 取消掉。

    唤醒    事务提交后协调层广播（租户，节点），各副本都收到；锁到节点会话行的那个干活
    扫描    定时任务（每个副本都跑）：排着的指令、要取消的 run，逐条进入各自的租户处理

一次投递：
    1. 锁节点会话行（FOR UPDATE SKIP LOCKED）：多副本不会同时投同一个节点；进程死了锁随连接释放
    2. 有要取消的 run → 以发起人的身份取消（已经结束的取消也算成功），清掉标记
    3. 会话不是 active（冻结、接管、停下）→ 不投
    4. 取队首指令：还没建会话就先建（数字员工，工作目录 = 流程空间）；以指令号作幂等键开 run，
       记 run 号。Agent 平台回「会话里还有没结束的 run」（刚取消、还在收尾）就留着，扫描时再投

★ 不跟随会话事件：TeamFlow 不知道 run 跑没跑完；下一条指令撞上没结束的 run 就等下一轮扫描。
★ 幂等：开 run 带指令号；在「开了 run」和「记下 run 号」之间挂掉，重投拿到同一个 run。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope, TenantSweep
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agentplatform import AgentPlatform, RunBusy
from ..db.types import utcnow
from ..errors import TFError
from ..outbound import acting_as
from ..process.service import ProcessService
from .models import Instruction, NodeSession

__all__ = ["InstructionDispatcher"]

log = logging.getLogger("atlas_teamflow.collab")


class InstructionDispatcher:
    def __init__(
        self,
        sessions: Callable[[], async_sessionmaker[AsyncSession]],
        atlas: AgentPlatform,
        process_of: Callable[[AsyncSession], ProcessService],
    ) -> None:
        self._sessions = sessions
        self.atlas = atlas
        self._process_of = process_of
        self._tasks: set[asyncio.Task[Any]] = set()

    # ───────────────────────────── 唤醒

    def on_wake(self, tenant: UUID, node_uuid: UUID) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（如同步脚本）：等扫描
        task = loop.create_task(self._deliver_quietly(tenant, node_uuid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """等已经排上的投递都跑完（测试、停机用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _deliver_quietly(self, tenant: UUID, node_uuid: UUID) -> None:
        try:
            with TenantScope.enter(tenant):
                await self.deliver(node_uuid)
        except Exception:
            log.exception("投递节点 %s 失败，等扫描重试", node_uuid)

    # ───────────────────────────── 扫描

    async def sweep(self) -> tuple[int, int]:
        """排着的指令、要取消的 run → 逐条进入租户投递。返回（成功，失败）。"""

        async def fetch() -> list[tuple[UUID, UUID]]:
            async with self._sessions()() as s:
                queued = exists().where(
                    Instruction.session_uuid == NodeSession.uuid, Instruction.status == "queued"
                )
                rows = await s.execute(
                    select(NodeSession.node_uuid, NodeSession.tenant_uuid).where(
                        or_(NodeSession.cancel_pending.is_(True), queued)
                    )
                )
                return [(n, t) for n, t in rows.all()]

        return await TenantSweep.each(fetch, self.deliver)

    # ───────────────────────────── 投递一个节点

    async def deliver(self, node_uuid: UUID) -> bool:
        """在当前租户里投递一个节点。返回这次是否做了事（取消或开了 run）。"""
        async with self._sessions()() as s:
            ns = await s.scalar(
                select(NodeSession)
                .where(NodeSession.node_uuid == node_uuid)
                .with_for_update(skip_locked=True)
            )
            if ns is None:
                return False  # 别的副本正在投，或者没有会话
            process = self._process_of(s)
            ctx = await process.node_context(node_uuid)
            did = False
            if ns.cancel_pending:
                if ns.last_run_id:
                    try:
                        with acting_as(ctx.started_by):
                            await self.atlas.cancel_run(ns.last_run_id)
                    except TFError as exc:
                        log.warning(
                            "取消节点 %s 的 run %s 失败，等扫描重试：%s",
                            node_uuid,
                            ns.last_run_id,
                            exc.message,
                        )
                        await s.commit()
                        return False
                ns.cancel_pending = False
                did = True
                await s.commit()
                ns = await s.scalar(
                    select(NodeSession).where(NodeSession.node_uuid == node_uuid).with_for_update()
                )
                assert ns is not None
            if ns.state != "active":
                await s.commit()
                return did
            ins = await s.scalar(
                select(Instruction)
                .where(Instruction.session_uuid == ns.uuid, Instruction.status == "queued")
                .order_by(Instruction.id)
                .limit(1)
            )
            if ins is None:
                await s.commit()
                return did
            if ctx.process_status != "running":
                ins.status = "cancelled"
                await s.commit()
                return did
            agent = ns.atlas_agent_id or ctx.atlas_agent_id
            if agent is None:
                ins.last_error = "执行角色没有可用的数字员工，请团队管理员在项目里给角色配数字员工"
                await s.commit()
                return did
            try:
                if ns.atlas_thread_id is None:
                    with acting_as(ctx.started_by):
                        ns.atlas_thread_id = await self.atlas.create_thread(
                            agent,
                            title=f"F-{ctx.process_no} {ctx.process_title} · {ctx.node_name}",
                            app_workspace=ctx.space_uuid,
                        )
                    ns.atlas_agent_id = agent
                    await s.commit()
                    ns = await s.scalar(
                        select(NodeSession)
                        .where(NodeSession.node_uuid == node_uuid)
                        .with_for_update()
                    )
                    assert ns is not None and ns.atlas_thread_id is not None
                with acting_as(ins.author or ctx.started_by):
                    run_id = await self.atlas.create_run(
                        ns.atlas_thread_id,
                        [{"type": "text", "text": ins.text}],
                        idempotency_key=str(ins.uuid),
                    )
            except RunBusy:
                ins.last_error = "会话里还有没结束的 run，稍后再投"
                await s.commit()
                return did
            except TFError as exc:
                ins.last_error = exc.message[:300]
                log.warning("投递节点 %s 的指令失败，等扫描重试：%s", node_uuid, exc.message)
                await s.commit()
                return did
            ins.status, ins.atlas_run_id, ins.sent_at, ins.last_error = (
                "sent",
                run_id,
                utcnow(),
                None,
            )
            ns.last_run_id = run_id
            await process.record(
                ctx.process_uuid,
                "instruction.sent",
                ctx.node_id,
                kind=ins.kind,
                round=ins.round,
                run=run_id,
            )
            await s.commit()
            return True
