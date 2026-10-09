"""Agent 适配层的监督器（系统设计 §10）：把节点会话映射为 Atlas 会话。

- 一个节点一个 Atlas 会话（thread）；人的一条指令 / 系统的开工上下文 = 一次运行（run）。
- 指令先在库里排队（tf_node_message.status = queued），事务提交后唤醒这里投递；
  同一节点串行：上一个 run 结束才投递下一条（Atlas 同一会话也只允许一个运行中的 run）。
- 订阅 Atlas 会话事件流：增量经进程内广播推给页面；run 结束时落库 Agent 回复，
  识别 <artifact>…</artifact> 并提交为产物版本。
- 断线按游标（thread_seq）续传，不判失败；服务重启后续接未完成的 run 与排队的指令。

★ 没有超时：Agent 运行可能持续数十分钟（atlas-no-timeouts 原则）。
★ 多实例：每个实例（API 内嵌或独立 Worker）都可以运行监督器。唤醒经协调层广播到全部实例，
  谁拿到节点租约锁谁干活；持有者每 lease/3 续租，进程死掉后租约过期，其他实例经定期扫描接手。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..atlas import AtlasClient
from ..db.models import ArtifactVersion, NodeMessage, Process, ProcessNode
from ..process.bus import LocalCoordinator, coordinator
from ..process.git_store import GitStore
from ..process.service import ProcessService

__all__ = ["AgentSupervisor", "extract_artifact"]

log = logging.getLogger("atlas_teamflow.agents")

_ARTIFACT = re.compile(r"<artifact(?:\s[^>]*)?>\s*\n?(.*?)\n?\s*</artifact>", re.DOTALL)
_TERMINAL = {"run.finished", "run.failed", "run.cancelled"}


def extract_artifact(text: str) -> str | None:
    """取最后一个 <artifact> 块的内容（Agent 每次输出完整全文）。"""
    hits = _ARTIFACT.findall(text or "")
    body = hits[-1].strip() if hits else ""
    return body or None


class AgentSupervisor:
    def __init__(
        self,
        sessionmaker: Callable[[], async_sessionmaker[AsyncSession]],
        atlas: AtlasClient,
        git: GitStore,
        *,
        retry_seconds: float = 3.0,
        lease_seconds: int = 30,
        sweep_seconds: float = 15.0,
        coord: LocalCoordinator | None = None,
    ) -> None:
        self._sm = sessionmaker
        self.atlas = atlas
        self.git = git
        self.retry_seconds = retry_seconds
        self.lease_seconds = lease_seconds
        self.sweep_seconds = sweep_seconds
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._again: set[UUID] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self.coord = coord or coordinator()
        self.coord.on_wake(self.wake)

    async def start(self) -> None:
        """续接重启前未完成的工作，并定期扫描（接管崩溃实例留下的节点、补漏掉的唤醒）。"""
        if self._sweeper is None:
            self._sweeper = asyncio.get_running_loop().create_task(self._sweep_forever())

    # ───────────────────────────── 调度

    def wake(self, node_uuid: UUID) -> None:
        task = self._tasks.get(node_uuid)
        if task and not task.done():
            self._again.add(node_uuid)  # 正在跑：跑完这一轮后再看一次队列
            return
        self._tasks[node_uuid] = asyncio.get_running_loop().create_task(self._drive(node_uuid))

    async def _sweep_forever(self) -> None:
        while True:
            try:
                await self.resume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("扫描待处理节点失败：%s", exc)
            await asyncio.sleep(self.sweep_seconds)

    async def resume(self) -> None:
        """有运行中 run 的节点、有排队指令的节点 → 本地唤醒（拿不到租约就是别的实例在处理）。"""
        async with self._sm()() as s:
            queued = select(NodeMessage.node_uuid).where(NodeMessage.status == "queued")
            ids = set(
                await s.scalars(
                    select(ProcessNode.uuid).where(
                        or_(ProcessNode.atlas_run_id.is_not(None), ProcessNode.uuid.in_(queued))
                    )
                )
            )
        fresh = [nid for nid in ids if nid not in self._tasks]
        for nid in fresh:
            self.wake(nid)
        if fresh:
            log.debug("扫描到 %d 个待处理节点", len(fresh))

    async def idle(self) -> None:
        """等所有在跑的投递结束（测试用）。"""
        while any(not t.done() for t in self._tasks.values()):
            await asyncio.gather(*[t for t in self._tasks.values() if not t.done()])

    async def close(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for t in list(self._tasks.values()):
            t.cancel()

    async def _drive(self, node_uuid: UUID) -> None:
        coord = self.coord
        token = uuid4().hex
        if not await coord.acquire(node_uuid, token):
            self._tasks.pop(node_uuid, None)
            return  # 别的实例正在处理这个节点
        me = asyncio.current_task()
        keeper = asyncio.get_running_loop().create_task(self._keep_lease(node_uuid, token, me))
        try:
            while True:
                self._again.discard(node_uuid)
                try:
                    more = await self._step(node_uuid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 不判失败：记下提示，稍后重试
                    log.warning("节点 %s 的 Agent 投递出错：%s", node_uuid, exc)
                    await self._notice(node_uuid, f"Agent 暂时无法连接，正在重试：{str(exc)[:80]}")
                    await asyncio.sleep(self.retry_seconds)
                    more = True
                if not more and node_uuid not in self._again:
                    return
        finally:
            keeper.cancel()
            self._tasks.pop(node_uuid, None)
            await coord.release(node_uuid, token)

    async def _keep_lease(
        self, node_uuid: UUID, token: str, owner: asyncio.Task[Any] | None
    ) -> None:
        """续租。续不上（租约已过期并被别人拿走）→ 停止本实例的处理，避免两边同时干活。"""
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            try:
                ok = await self.coord.renew(node_uuid, token)
            except Exception as exc:
                log.warning("节点 %s 续租出错：%s", node_uuid, exc)
                continue
            if not ok:
                log.warning("节点 %s 的租约已失去，停止处理", node_uuid)
                if owner:
                    owner.cancel()
                return

    # ───────────────────────────── 一步：投递一条指令并跟到 run 结束

    async def _step(self, node_uuid: UUID) -> bool:
        async with self._sm()() as s:
            node = (
                await s.execute(select(ProcessNode).where(ProcessNode.uuid == node_uuid))
            ).scalar_one_or_none()
            if node is None:
                return False
            proc = (
                await s.execute(select(Process).where(Process.uuid == node.process_uuid))
            ).scalar_one()
            if node.atlas_run_id is None:
                msg = (
                    await s.execute(
                        select(NodeMessage)
                        .where(NodeMessage.node_uuid == node.uuid, NodeMessage.status == "queued")
                        .order_by(NodeMessage.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if msg is None:
                    return False
                if proc.status != "running" or node.atlas_agent_id is None:
                    msg.status = "done"  # 流程已结束 / 没有 Agent：不再投递
                    await s.commit()
                    return True
                if node.atlas_thread_id is None:
                    node.atlas_thread_id = await self.atlas.create_thread(
                        node.atlas_agent_id, f"[TeamFlow] {proc.title} · {node.name}"
                    )
                    await s.commit()
                run_id = await self.atlas.create_run(
                    node.atlas_thread_id, msg.content, idempotency_key=str(msg.uuid)
                )
                msg.status, msg.atlas_run_id = "done", run_id
                node.atlas_run_id = run_id
                if node.notice and node.notice.startswith("Agent"):
                    node.notice = None
                exists = await s.scalar(
                    select(NodeMessage.id).where(
                        NodeMessage.node_uuid == node.uuid,
                        NodeMessage.atlas_run_id == run_id,
                        NodeMessage.role == "agent",
                    )
                )
                if exists is None:  # 幂等：重投同一条指令拿到的是同一个 run
                    s.add(
                        NodeMessage(
                            node_uuid=node.uuid,
                            role="agent",
                            content="",
                            atlas_run_id=run_id,
                            status="streaming",
                            round=node.round,
                        )
                    )
                svc = ProcessService.system(s, self.git)
                await s.execute(
                    select(Process.id).where(Process.uuid == proc.uuid).with_for_update()
                )
                svc.emit(proc, "agent.started", node.node_id, run=run_id)
                await s.commit()
            thread_id, run_id, cursor = node.atlas_thread_id, node.atlas_run_id, node.atlas_cursor
            proc_uuid, node_id = proc.uuid, node.node_id
        assert thread_id and run_id
        result = await self._follow(thread_id, run_id, cursor, proc_uuid, node_uuid, node_id)
        await self._finish(node_uuid, run_id, *result)
        return True

    async def _follow(
        self,
        thread_id: str,
        run_id: str,
        cursor: int,
        proc_uuid: UUID,
        node_uuid: UUID,
        node_id: str,
    ) -> tuple[str, str, int, str | None]:
        """跟随事件流到本 run 终态 → (终态, 正文, 游标, 错误)。断线按游标续传。"""
        deltas: dict[int, str] = {}
        text: str | None = None
        while True:
            try:
                async for ev in self.atlas.stream_thread(thread_id, after_seq=cursor):
                    cursor = max(cursor, ev.thread_seq)
                    if ev.run_id != run_id:
                        continue
                    if ev.type == "message.delta":
                        b = int(ev.data.get("block") or 0)
                        deltas[b] = deltas.get(b, "") + str(ev.data.get("text") or "")
                        self.coord.publish(
                            proc_uuid,
                            {
                                "kind": "delta",
                                "node_id": node_id,
                                "run_id": run_id,
                                "block": b,
                                "text": ev.data.get("text") or "",
                            },
                        )
                    elif ev.type == "message.completed":
                        blocks = ev.data.get("content") or []
                        text = "\n\n".join(
                            str(b.get("text") or "") for b in blocks if b.get("type") == "text"
                        )
                    elif ev.type == "approval.required":
                        await self._notice(
                            node_uuid, "Agent 在 Atlas 中等待操作确认，请到 Atlas 会话中处理"
                        )
                    elif ev.type in _TERMINAL:
                        body = (
                            text
                            if text is not None
                            else "\n\n".join(deltas[k] for k in sorted(deltas))
                        )
                        error = ev.data.get("message") if ev.type != "run.finished" else None
                        return ev.type, body, cursor, error
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info(
                    "Atlas 事件流断开，%ss 后按游标 %s 续传：%s", self.retry_seconds, cursor, exc
                )
            await asyncio.sleep(self.retry_seconds)

    async def _finish(
        self, node_uuid: UUID, run_id: str, kind: str, body: str, cursor: int, error: str | None
    ) -> None:
        async with self._sm()() as s:
            node = (
                await s.execute(select(ProcessNode).where(ProcessNode.uuid == node_uuid))
            ).scalar_one()
            proc = (
                await s.execute(
                    select(Process).where(Process.uuid == node.process_uuid).with_for_update()
                )
            ).scalar_one()
            msg = (
                await s.execute(
                    select(NodeMessage)
                    .where(
                        NodeMessage.node_uuid == node.uuid,
                        NodeMessage.atlas_run_id == run_id,
                        NodeMessage.role == "agent",
                    )
                    .order_by(NodeMessage.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if node.atlas_run_id != run_id:
                return  # 已被收尾（极端情况下的重复跟随）：不重复落库
            ok = kind == "run.finished"
            if msg is not None:
                msg.content = body or ("（Agent 没有输出）" if ok else "")
                msg.status = "done" if ok else "failed"
            node.atlas_run_id = None
            node.atlas_cursor = cursor
            svc = ProcessService.system(s, self.git)
            if not ok:
                what = "被取消" if kind == "run.cancelled" else "失败"
                node.notice = f"Agent 运行{what}：{(error or '')[:120]}"
                svc.emit(proc, "agent.failed", node.node_id, error=(error or "")[:200])
            else:
                svc.emit(proc, "agent.replied", node.node_id)
                content = extract_artifact(body)
                if content and node.status == "working" and proc.status == "running":
                    cur = None
                    if node.current_artifact_uuid:
                        cur = (
                            await s.execute(
                                select(ArtifactVersion.content, ArtifactVersion.round).where(
                                    ArtifactVersion.uuid == node.current_artifact_uuid
                                )
                            )
                        ).one()
                    # 同一轮里重复输出相同全文不重复提交；新的一轮即使内容相同也记一个版本
                    if cur is None or cur[0].strip() != content.strip() or cur[1] != node.round:
                        await svc.commit_artifact(proc, node, content, source="agent", author=None)
            await s.commit()

    async def _notice(self, node_uuid: UUID, text: str) -> None:
        async with self._sm()() as s:
            node = (
                await s.execute(select(ProcessNode).where(ProcessNode.uuid == node_uuid))
            ).scalar_one_or_none()
            if node is None:
                return
            node.notice = text[:200]
            proc_uuid = node.process_uuid
            await s.commit()
        self.coord.nudge(proc_uuid)
