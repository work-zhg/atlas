"""NodeCollab：节点协作的唯一入口（c4-model/teamflow/code-collab.html）。

一个节点一个 Agent 平台会话，挂这条流程的流程空间，跨轮次复用。开 run 都经这里：
    系统指令    流程运行经 NodeWork 说「开工、冻结、停下」：在调用方的事务里排指令、标状态，
                提交后由投递器（dispatcher.py）开 run、取消 run
    人发的消息  节点对话接口核对执行人后，当场以作者的身份开 run，也记一条指令
    接管、交还  接管：挡住开新 run、取消最后开的 run；交还：把说明排成下一条指令

★ 看对话、回答提问和审批、接管浏览器、停止都由节点页直连 Agent 平台，不经这里；TeamFlow 不跟随
  会话事件，只记「哪个 run 属于哪个节点」，停下节点时知道取消哪一个。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agentplatform import AgentPlatform
from ..coord import wake_after_commit
from ..db.types import utcnow
from ..errors import Conflict, Invalid
from ..outbound import acting_as
from ..process.models import ProcessNode
from ..process.port import WorkOrder
from ..process.service import ProcessService
from .brief import render, render_handback
from .models import Instruction, NodeSession

__all__ = ["NodeCollab"]


class NodeCollab:
    """实现 NodeWork。"""

    def __init__(
        self, session: AsyncSession, process: ProcessService, atlas: AgentPlatform
    ) -> None:
        self.session = session
        self.process = process
        self.atlas = atlas

    async def session_of(self, node_uuid: UUID) -> NodeSession | None:
        return await self.session.scalar(
            select(NodeSession).where(NodeSession.node_uuid == node_uuid)
        )

    async def _ensure(self, node: ProcessNode) -> NodeSession:
        ns = await self.session_of(node.uuid)
        if ns is None:
            ns = NodeSession(
                node_uuid=node.uuid,
                process_uuid=node.process_uuid,
                atlas_agent_id=node.atlas_agent_id,
            )
            self.session.add(ns)
            await self.session.flush()
        elif ns.atlas_agent_id is None and node.atlas_agent_id:
            ns.atlas_agent_id = node.atlas_agent_id
        return ns

    async def _cancel_queued(self, ns: NodeSession) -> None:
        await self.session.execute(
            update(Instruction)
            .where(Instruction.session_uuid == ns.uuid, Instruction.status == "queued")
            .values(status="cancelled")
        )

    # ───────────────────────────── NodeWork（在调用方的事务里）

    async def kickoff(self, node: ProcessNode, order: WorkOrder) -> None:
        ns = await self._ensure(node)
        ns.state, ns.taken_by = "active", None
        self.session.add(
            Instruction(
                session_uuid=ns.uuid,
                kind="kickoff" if order.round <= 1 else "revise",
                round=order.round,
                text=render(order),
            )
        )
        wake_after_commit(self.session.sync_session, node.uuid)

    async def freeze(self, node: ProcessNode) -> None:
        await self._halt(node, "frozen")

    async def stop(self, nodes: list[ProcessNode]) -> None:
        for n in nodes:
            await self._halt(n, "stopped")

    async def _halt(self, node: ProcessNode, state: str) -> None:
        ns = await self.session_of(node.uuid)
        if ns is None:
            return
        ns.state = state
        await self._cancel_queued(ns)
        if ns.last_run_id:
            ns.cancel_pending = True
            wake_after_commit(self.session.sync_session, node.uuid)

    # ───────────────────────────── 节点对话接口

    async def can_send(self, node: ProcessNode, ns: NodeSession | None) -> bool:
        return (
            node.status == "working"
            and ns is not None
            and ns.state == "active"
            and ns.atlas_thread_id is not None
        )

    async def send_human(
        self,
        process_uuid: UUID,
        node_id: str,
        content: list[dict[str, Any]],
        *,
        author: UUID,
        idem: str,
    ) -> str:
        """人在对话框里发的消息：当场以作者的身份开 run，记一条 HUMAN 指令。执行人由入口判过。"""
        text = "\n".join(
            str(b.get("text") or "") for b in content if b.get("type") == "text"
        ).strip()
        if not content or (not text and not any(b.get("type") != "text" for b in content)):
            raise Invalid("EMPTY_MESSAGE", "消息不能为空")
        proc = await self.process.get(process_uuid)
        node = await self.process.node(proc, node_id)
        ns = await self.session_of(node.uuid)
        if proc.status != "running" or node.status != "working":
            raise Conflict("NODE_NOT_WORKING", "节点不在人机协同中，不能发消息")
        if ns is None or ns.atlas_thread_id is None:
            raise Conflict("SESSION_NOT_READY", "数字员工还没开始，请稍等")
        if ns.state == "taken_over":
            raise Conflict("NODE_TAKEN_OVER", "节点已被接管，交还后才能发消息")
        if ns.state != "active":
            raise Conflict("NODE_FROZEN", "节点在评审中，不能发消息")
        existing = await self.session.scalar(
            select(Instruction).where(
                Instruction.session_uuid == ns.uuid, Instruction.uuid == _idem_uuid(idem)
            )
        )
        if existing and existing.atlas_run_id:
            return existing.atlas_run_id
        with acting_as(author):
            run_id = await self.atlas.create_run(ns.atlas_thread_id, content, idempotency_key=idem)
        ins = Instruction(
            uuid=_idem_uuid(idem),
            session_uuid=ns.uuid,
            kind="human",
            round=node.round,
            author=author,
            text=text[:20000],
            status="sent",
            atlas_run_id=run_id,
            sent_at=utcnow(),
        )
        self.session.add(ins)
        ns.last_run_id = run_id
        await self.process.record(proc.uuid, "message.sent", node.node_id, actor=author, run=run_id)
        await self.session.flush()
        return run_id

    async def take_over(self, process_uuid: UUID, node_id: str, *, actor: UUID) -> None:
        proc = await self.process.get(process_uuid, lock=True)
        node = await self.process.node(proc, node_id)
        if proc.status != "running" or node.status != "working":
            raise Conflict("NODE_NOT_WORKING", "节点不在人机协同中")
        ns = await self._ensure(node)
        if ns.state == "taken_over":
            raise Conflict("ALREADY_TAKEN_OVER", "节点已经被接管了")
        if ns.state != "active":
            raise Conflict("NODE_FROZEN", "节点在评审中，不能接管")
        ns.state, ns.taken_by = "taken_over", actor
        await self._cancel_queued(ns)
        if ns.last_run_id:
            ns.cancel_pending = True
            wake_after_commit(self.session.sync_session, node.uuid)
        self.process.emit(proc, "node.taken_over", node.node_id, actor=actor)
        await self.session.flush()

    async def hand_back(self, process_uuid: UUID, node_id: str, note: str, *, actor: UUID) -> None:
        proc = await self.process.get(process_uuid, lock=True)
        node = await self.process.node(proc, node_id)
        ns = await self.session_of(node.uuid)
        if ns is None or ns.state != "taken_over":
            raise Conflict("NOT_TAKEN_OVER", "节点没有被接管")
        ns.state, ns.taken_by = "active", None
        self.session.add(
            Instruction(
                session_uuid=ns.uuid,
                kind="handback",
                round=node.round,
                author=actor,
                text=render_handback(node.name, note or ""),
            )
        )
        wake_after_commit(self.session.sync_session, node.uuid)
        self.process.emit(
            proc, "node.handed_back", node.node_id, actor=actor, note=(note or "")[:500]
        )
        await self.session.flush()

    async def instructions(self, node_uuid: UUID) -> list[Instruction]:
        ns = await self.session_of(node_uuid)
        if ns is None:
            return []
        return list(
            await self.session.scalars(
                select(Instruction)
                .where(Instruction.session_uuid == ns.uuid)
                .order_by(Instruction.id)
            )
        )


def _idem_uuid(idem: str) -> UUID:
    """幂等键 → 指令号：同一个键重发拿到同一行（对话组件生成 uuid；不是 uuid 的按内容派生）。"""
    import uuid as _uuid

    try:
        return UUID(idem)
    except ValueError:
        return _uuid.uuid5(_uuid.NAMESPACE_URL, f"teamflow:human:{idem}")
