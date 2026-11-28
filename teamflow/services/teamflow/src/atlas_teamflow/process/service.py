"""ProcessService：流程运行的唯一入口（c4-model/teamflow/code-process.html）。

节点七种状态：待开始 → 人机协同中 → 准出评审中 → 待下游准入 → 已通过；准入评审中、已打回。
准入是「下阶段准入」：上游准出后由下游的准入评审人审它交的东西，下游准入通过才开工；上游的全部
下游都开工了，上游才算通过；最后一个节点准出，流程完成。

★ 首节点（没有上游的节点）发起后停在待开始，等执行人往流程空间传好资料、点「开始」（begin）。
★ 改状态的方法都先锁流程行（SELECT … FOR UPDATE），同一条流程的推进串行。解析角色要问用户中心，
  在锁之前算好，锁里只用结果。
★ 三处锁定：发起锁模板版本；节点第一次开工锁数字员工；进评审锁评审人名单。执行人不锁，按项目
  当前分配。
★ 节点开工、冻结、停下只经 NodeWork 说「要做什么」，节点协作在提交后才调 Agent 平台。
★ 没有超时：协同和评审可能持续几天。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import structure
from ..audit import record
from ..coord import nudge_after_commit
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..projects.service import ProjectService, RoleTable
from ..space.service import FlowSpaces, SpacePath
from ..templates.service import TemplateService
from .models import Process, ProcessEvent, ProcessNode, ReviewRound, Submission, Vote
from .port import NodeWork, WorkOrder

__all__ = ["NO_BASE", "NodeContext", "ProcessService"]

#: 流程编号从这里往上数，显示成 F-1001、F-1002…
NO_BASE = 1000
STARTED = ("working", "exit_review", "exited", "passed")


def _text(value: str | None, label: str, max_len: int) -> str:
    v = (value or "").strip()
    if not v:
        raise Invalid("VALIDATION_FAILED", f"请填写{label}")
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符")
    return v


class NodeContext:
    """节点协作要的节点上下文：流程、空间、发起人、节点名、执行的数字员工。"""

    def __init__(self, proc: Process, node: ProcessNode) -> None:
        self.process_uuid = proc.uuid
        self.process_no = proc.no
        self.process_title = proc.title
        self.process_status = proc.status
        self.space_uuid = proc.space_uuid
        self.started_by = proc.started_by
        self.node_uuid = node.uuid
        self.node_id = node.node_id
        self.node_name = node.name
        self.node_status = node.status
        self.exec_role = node.exec_role
        self.atlas_agent_id = node.atlas_agent_id


class ProcessService:
    def __init__(
        self,
        session: AsyncSession,
        projects: ProjectService,
        templates: TemplateService,
        spaces: FlowSpaces,
        work: NodeWork | None = None,
    ) -> None:
        self.session = session
        self.projects = projects
        self.templates = templates
        self.spaces = spaces
        #: 节点协作在组装时放进来（它也依赖流程运行，见 api/deps.py）
        self.work: NodeWork = work  # type: ignore[assignment]

    # ───────────────────────────── 读取

    async def get(self, process_uuid: UUID, *, lock: bool = False) -> Process:
        stmt = select(Process).where(Process.uuid == process_uuid)
        if lock:
            stmt = stmt.with_for_update()
        proc = await self.session.scalar(stmt)
        if proc is None:
            raise NotFound("PROCESS_NOT_FOUND", "流程不存在")
        return proc

    async def structure_of(self, proc: Process) -> dict[str, Any]:
        return (await self.templates.version(proc.template_version_uuid)).structure

    async def nodes(self, proc: Process) -> dict[str, ProcessNode]:
        rows = await self.session.scalars(
            select(ProcessNode).where(ProcessNode.process_uuid == proc.uuid)
        )
        return {n.node_id: n for n in rows}

    async def node(self, proc: Process, node_id: str) -> ProcessNode:
        n = await self.session.scalar(
            select(ProcessNode).where(
                ProcessNode.process_uuid == proc.uuid, ProcessNode.node_id == node_id
            )
        )
        if n is None:
            raise NotFound("NODE_NOT_FOUND", "节点不存在")
        return n

    async def node_by_uuid(self, node_uuid: UUID) -> tuple[Process, ProcessNode]:
        n = await self.session.scalar(select(ProcessNode).where(ProcessNode.uuid == node_uuid))
        if n is None:
            raise NotFound("NODE_NOT_FOUND", "节点不存在")
        return await self.get(n.process_uuid), n

    async def roles(self, proc: Process) -> RoleTable:
        """现在谁是这条流程的各个角色（按锁定版本的角色表、项目当前的分配）。会问用户中心：锁外调。"""
        return await self.projects.resolve(
            proc.project_uuid, proc.template_uuid, roles_of=await self.structure_of(proc)
        )

    async def executors(self, proc: Process, node: ProcessNode) -> list[UUID]:
        return (await self.roles(proc)).members.get(node.exec_role, [])

    def _require_running(self, proc: Process) -> None:
        if proc.status != "running":
            raise Conflict("PROCESS_NOT_RUNNING", "流程已结束")

    @staticmethod
    def _require_executor(table: RoleTable, node: ProcessNode, actor: UUID) -> None:
        if actor not in table.members.get(node.exec_role, []):
            raise Forbidden("NOT_EXECUTOR", "只有这个节点的执行人可以做这件事")

    # ───────────────────────────── 事件

    def emit(
        self,
        proc: Process,
        type_: str,
        node_id: str | None = None,
        *,
        actor: UUID | None = None,
        **data: Any,
    ) -> None:
        proc.event_seq += 1
        self.session.add(
            ProcessEvent(
                process_uuid=proc.uuid,
                seq=proc.event_seq,
                type=type_,
                node_id=node_id,
                actor=actor,
                data=_jsonable(data),
            )
        )
        nudge_after_commit(self.session.sync_session, proc.uuid)

    async def record(
        self,
        process_uuid: UUID,
        type_: str,
        node_id: str | None = None,
        *,
        actor: UUID | None = None,
        **data: Any,
    ) -> None:
        """别的领域（节点协作、入口的上传）记一条流程事件：锁流程行，和它们的写同一个事务。"""
        proc = await self.get(process_uuid, lock=True)
        self.emit(proc, type_, node_id, actor=actor, **data)

    # ───────────────────────────── 发起

    async def start(
        self, project_uuid: UUID, template_uuid: UUID, title: str, *, actor: UUID
    ) -> Process:
        """配齐 → 授使用权 → 申请流程空间（锁外，空间登记会先提交）→ 一个事务里建流程和节点。

        节点都停在待开始：首节点等执行人点「开始」。
        """
        title = _text(title, "流程标题", 100)
        project = await self.projects.get(project_uuid)
        if template_uuid not in await self.projects.launchable(project.uuid):
            raise Conflict(
                "TEMPLATE_NOT_READY", "这个模板还没配齐角色，或者项目、团队已归档，不能发起"
            )
        version = await self.templates.latest_published(template_uuid)
        assert version is not None
        await self.projects.grant_use(project.uuid, template_uuid, roles_of=version.structure)
        space = await self.spaces.open(title=title, actor=actor)
        proc = Process(
            title=title,
            no=0,
            team_uuid=project.team_uuid,
            project_uuid=project.uuid,
            template_uuid=template_uuid,
            template_version_uuid=version.uuid,
            space_uuid=space.uuid,
            started_by=actor,
        )
        await self._insert_numbered(proc)
        for nid in structure.node_ids(version.structure):
            spec = version.structure["nodes"][nid]
            self.session.add(
                ProcessNode(
                    process_uuid=proc.uuid,
                    node_id=nid,
                    name=spec["name"],
                    exec_role=spec["exec_role"],
                )
            )
        await self.spaces.bind(space.uuid, proc.uuid)
        self.emit(proc, "process.started", actor=actor, title=title, version=version.version_no)
        record(self.session, actor, "process.start", "process", proc.uuid, title)
        await self.session.flush()
        return proc

    async def _insert_numbered(self, proc: Process) -> None:
        """编号 = 租户内最大 + 1；并发撞唯一约束就重取。"""
        for _ in range(8):
            top = await self.session.scalar(select(func.max(Process.no)))
            proc.no = int(top or NO_BASE) + 1
            try:
                async with self.session.begin_nested():
                    self.session.add(proc)
                    await self.session.flush()
                return
            except IntegrityError:
                continue
        raise Conflict("PROCESS_NO_BUSY", "同时发起的流程太多，请重试")

    async def begin(self, process_uuid: UUID, node_id: str, *, actor: UUID) -> ProcessNode:
        """首节点由执行人点「开始」：这时才锁数字员工、排开工指令。"""
        pre = await self.get(process_uuid)
        table = await self.roles(pre)
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        s = await self.structure_of(proc)
        n = await self.node(proc, node_id)
        if node_id not in structure.roots(s):
            raise Conflict("NOT_ROOT_NODE", "只有第一个节点要手动开始，后面的节点按准入自动开工")
        if n.status != "pending":
            raise Conflict("NODE_ALREADY_STARTED", "这个节点已经开始了")
        self._require_executor(table, n, actor)
        await self._start_node(proc, n, s, table, await self.nodes(proc), actor=actor)
        await self.session.flush()
        return n

    # ───────────────────────────── 推进

    async def _start_node(
        self,
        proc: Process,
        n: ProcessNode,
        s: dict[str, Any],
        table: RoleTable,
        nodes: dict[str, ProcessNode],
        *,
        feedback: str | None = None,
        actor: UUID | None = None,
    ) -> None:
        n.status = "working"
        n.round += 1
        if n.started_at is None:
            n.started_at = utcnow()
        if n.atlas_agent_id is None:
            agent = table.agents.get(n.exec_role)
            n.atlas_agent_id = agent[1] if agent else None
        spec = s["nodes"][n.node_id]
        order = WorkOrder(
            node_name=n.name,
            instructions=spec.get("instructions") or "",
            round=n.round,
            title=proc.title,
            upstream=await self._upstream(s, nodes, n),
            feedback=feedback,
            materials=await self._materials(proc),
        )
        await self.work.kickoff(n, order)
        self.emit(
            proc,
            "node.started",
            n.node_id,
            actor=actor,
            round=n.round,
            agent=n.atlas_agent_id,
            to=_uids(table.members.get(n.exec_role, [])),
        )

    async def _upstream(
        self, s: dict[str, Any], nodes: dict[str, ProcessNode], n: ProcessNode
    ) -> list[tuple[str, list[str]]]:
        out = []
        for up in structure.deps(s).get(n.node_id, []):
            sub = await self.latest_submission(nodes[up])
            if sub is not None:
                out.append((nodes[up].name, list(sub.paths)))
        return out

    async def _materials(self, proc: Process) -> list[str]:
        """成员上传过的文件（按上传先后，去重）。"""
        seen: dict[str, None] = {}
        for e in await self.events(proc, types=["space.uploaded"], limit=5000):
            path = e.data.get("path")
            if isinstance(path, str) and path:
                seen.setdefault(path, None)
        return list(seen)

    async def latest_submission(self, n: ProcessNode) -> Submission | None:
        return await self.session.scalar(
            select(Submission)
            .where(Submission.node_uuid == n.uuid)
            .order_by(Submission.round.desc())
            .limit(1)
        )

    async def _advance(self, proc: Process, s: dict[str, Any], table: RoleTable) -> None:
        """上游都准出（或已通过）的待开始 / 已打回节点：有准入就开准入评审，没有就开工。
        首节点没有上游，不在这里开。全部通过 → 流程完成。"""
        nodes = await self.nodes(proc)
        for nid, ups in structure.deps(s).items():
            n = nodes[nid]
            if not ups or n.status not in ("pending", "returned"):
                continue
            if not all(nodes[u].status in ("exited", "passed") for u in ups):
                continue
            admit = s["nodes"][nid].get("admit")
            if admit and any(nodes[u].status == "exited" for u in ups):
                await self._open_review(proc, n, "admit", admit, table, nodes)
            else:
                await self._start_node(proc, n, s, table, nodes)
                self._settle_upstreams(proc, s, nodes, nid)
        if nodes and all(n.status == "passed" for n in nodes.values()):
            proc.status, proc.finished_at = "completed", utcnow()
            await self.spaces.seal(proc.space_uuid)
            self.emit(proc, "process.completed", to=[str(proc.started_by)])

    def _settle_upstreams(
        self, proc: Process, s: dict[str, Any], nodes: dict[str, ProcessNode], nid: str
    ) -> None:
        """nid 开工后：它的上游里「全部下游都已开工」的待下游准入节点 → 已通过。"""
        for u in structure.deps(s).get(nid, []):
            up = nodes[u]
            if up.status != "exited":
                continue
            if all(nodes[d].status in STARTED for d in structure.downstreams(s, u)):
                up.status, up.passed_at = "passed", utcnow()
                self.emit(proc, "node.passed", u)

    # ───────────────────────────── 评审

    async def open_review(self, n: ProcessNode) -> ReviewRound | None:
        return await self.session.scalar(
            select(ReviewRound).where(ReviewRound.node_uuid == n.uuid, ReviewRound.status == "open")
        )

    async def _open_review(
        self,
        proc: Process,
        n: ProcessNode,
        stage: str,
        spec: dict[str, Any],
        table: RoleTable,
        nodes: dict[str, ProcessNode],
        *,
        submission: Submission | None = None,
    ) -> ReviewRound:
        reviewers: list[UUID] = []
        for role in spec.get("roles") or []:
            reviewers.extend(u for u in table.members.get(role, []) if u not in reviewers)
        if stage == "exit":
            subs = [str(submission.uuid)] if submission else []
        else:
            s = await self.structure_of(proc)
            subs = []
            for up in structure.deps(s).get(n.node_id, []):
                last = await self.latest_submission(nodes[up])
                if last is not None:
                    subs.append(str(last.uuid))
        rr = ReviewRound(
            node_uuid=n.uuid,
            stage=stage,
            # 准入发生在本节点开工之前：记为即将开始的那一轮
            round=n.round if stage == "exit" else n.round + 1,
            roles=list(spec.get("roles") or []),
            rule=spec.get("rule", "any"),
            reviewers=[str(u) for u in reviewers],
            submissions=subs,
        )
        self.session.add(rr)
        n.status = "exit_review" if stage == "exit" else "admit_review"
        await self.session.flush()
        self.emit(
            proc,
            "review.opened",
            n.node_id,
            stage=stage,
            rule=rr.rule,
            reviewers=len(reviewers),
            to=_uids(reviewers),
        )
        return rr

    async def submit(
        self, process_uuid: UUID, node_id: str, paths: list[str], *, actor: UUID
    ) -> ReviewRound:
        """提交准出评审：从流程空间选文件（至少一个、都在），只记路径；锁评审人名单；冻结节点会话。

        评审人名单为空（角色没人）的评审可以重新提交，换成当前的名单。
        """
        clean = list(dict.fromkeys(SpacePath.of(p) for p in paths or []))
        if not clean:
            raise Invalid("NO_FILES", "至少选一个文件")
        pre = await self.get(process_uuid)
        missing = await self.spaces.exists(pre.space_uuid, clean)
        if missing:
            raise Invalid("FILES_MISSING", "这些文件不在流程空间里", missing=missing)
        table = await self.roles(pre)
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        s = await self.structure_of(proc)
        n = await self.node(proc, node_id)
        self._require_executor(table, n, actor)
        spec = s["nodes"][n.node_id]["exit"]
        nodes = await self.nodes(proc)
        if n.status == "exit_review":
            rr = await self.open_review(n)
            if rr is None or rr.reviewers:
                raise Conflict("ALREADY_IN_REVIEW", "节点已经在评审中")
            rr.status, rr.closed_at = "cancelled", utcnow()
        elif n.status != "working":
            raise Conflict("NODE_NOT_WORKING", "节点不在人机协同中")
        sub = await self.session.scalar(
            select(Submission).where(Submission.node_uuid == n.uuid, Submission.round == n.round)
        )
        if sub is None:
            sub = Submission(node_uuid=n.uuid, round=n.round, paths=clean, submitted_by=actor)
            self.session.add(sub)
        else:
            sub.paths, sub.submitted_by = clean, actor
        await self.session.flush()
        self.emit(proc, "submission.created", n.node_id, actor=actor, round=n.round, paths=clean)
        rr = await self._open_review(proc, n, "exit", spec, table, nodes, submission=sub)
        await self.work.freeze(n)
        await self.session.flush()
        return rr

    async def vote(
        self,
        process_uuid: UUID,
        node_id: str,
        decision: str,
        comment: str | None,
        return_to: str | None,
        *,
        actor: UUID,
    ) -> ProcessNode:
        if decision not in ("approve", "reject"):
            raise Invalid("VALIDATION_FAILED", "decision 只能是 approve 或 reject")
        pre = await self.get(process_uuid)
        table = await self.roles(pre)
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        s = await self.structure_of(proc)
        n = await self.node(proc, node_id)
        rr = await self.open_review(n)
        if rr is None:
            raise Conflict("NO_OPEN_REVIEW", "节点不在评审中")
        if str(actor) not in rr.reviewers:
            raise Forbidden("NOT_REVIEWER", "你不在这一轮的评审名单里")
        if await self.session.scalar(
            select(Vote.id).where(Vote.review_uuid == rr.uuid, Vote.reviewer == actor)
        ):
            raise Conflict("ALREADY_VOTED", "你已经表过态了")
        deps = structure.deps(s)
        if decision == "reject":
            comment = _text(comment, "意见", 5000)
            if rr.stage == "admit" and not return_to:
                ups = deps.get(n.node_id, [])
                if len(ups) != 1:
                    raise Invalid("RETURN_TARGET_REQUIRED", "准入不通过要选打回到哪个上游节点")
                return_to = ups[0]
            if return_to and return_to not in structure.ancestors(s, n.node_id):
                raise Invalid("INVALID_RETURN_TARGET", "只能打回到本节点的上游节点")
        self.session.add(
            Vote(
                review_uuid=rr.uuid,
                reviewer=actor,
                decision=decision,
                comment=(comment or "").strip() or None,
                return_to=return_to if decision == "reject" else None,
            )
        )
        self.emit(
            proc,
            "vote.cast",
            n.node_id,
            actor=actor,
            stage=rr.stage,
            decision=decision,
            comment=(comment or "").strip()[:300] or None,
        )
        await self.session.flush()
        nodes = await self.nodes(proc)
        if decision == "reject":
            rr.status, rr.closed_at = "rejected", utcnow()
            self.emit(
                proc,
                "review.rejected",
                n.node_id,
                actor=actor,
                stage=rr.stage,
                return_to=return_to,
                comment=(comment or "")[:300] or None,
            )
            if return_to:
                await self._send_back(
                    proc, s, table, nodes, n, return_to, comment or "", actor=actor
                )
            else:
                # 准出驳回：本节点下一轮，意见进工作单
                await self._start_node(proc, n, s, table, nodes, feedback=comment, actor=actor)
            await self.session.flush()
            return n
        approvals = int(
            await self.session.scalar(
                select(func.count())
                .select_from(Vote)
                .where(Vote.review_uuid == rr.uuid, Vote.decision == "approve")
            )
            or 0
        )
        if rr.rule == "any" or approvals >= len(rr.reviewers):
            rr.status, rr.closed_at = "approved", utcnow()
            self.emit(proc, "review.approved", n.node_id, stage=rr.stage)
            if rr.stage == "admit":
                await self._start_node(proc, n, s, table, nodes)
                self._settle_upstreams(proc, s, nodes, n.node_id)
            elif structure.downstreams(s, n.node_id):
                n.status = "exited"
                self.emit(proc, "node.exited", n.node_id)
            else:
                n.status, n.passed_at = "passed", utcnow()
                self.emit(proc, "node.passed", n.node_id)
            await self.session.flush()
            await self._advance(proc, s, table)
        await self.session.flush()
        return n

    async def _send_back(
        self,
        proc: Process,
        s: dict[str, Any],
        table: RoleTable,
        nodes: dict[str, ProcessNode],
        by: ProcessNode,
        target_id: str,
        comment: str,
        *,
        actor: UUID,
    ) -> None:
        """打回：目标回到人机协同中（下一轮，意见进工作单）；它已开工、在准入的后代 → 已打回，
        停下数字员工、取消进行中的评审。目标重新准出后，这些后代重新走准入。"""
        target = nodes[target_id]
        stopped: list[ProcessNode] = []
        for did in structure.descendants(s, target_id):
            d = nodes[did]
            if d.status == "pending":
                continue
            open_rr = await self.open_review(d)
            if open_rr:
                open_rr.status, open_rr.closed_at = "cancelled", utcnow()
            if d.started_at is not None:
                stopped.append(d)
            d.status, d.passed_at = "returned", None
        if stopped:
            await self.work.stop(stopped)
        target.passed_at = None
        await self._start_node(proc, target, s, table, nodes, feedback=comment, actor=actor)
        self.emit(
            proc,
            "node.returned",
            target_id,
            actor=actor,
            by=by.node_id,
            comment=comment[:500],
            to=_uids(table.members.get(target.exec_role, [])),
        )

    # ───────────────────────────── 终止

    async def terminate(
        self, process_uuid: UUID, reason: str | None, *, actor: UUID, is_owner: bool
    ) -> Process:
        """发起人或团队 Owner 可以终止：进行中的评审取消、数字员工停下、流程空间转只读。"""
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        if proc.started_by != actor and not is_owner:
            raise Forbidden("TERMINATE_DENIED", "只有发起人或团队管理员能终止流程")
        proc.status, proc.finished_at = "terminated", utcnow()
        started = []
        for n in (await self.nodes(proc)).values():
            rr = await self.open_review(n)
            if rr:
                rr.status, rr.closed_at = "cancelled", utcnow()
            if n.started_at is not None:
                started.append(n)
        if started:
            await self.work.stop(started)
        await self.spaces.seal(proc.space_uuid)
        self.emit(
            proc, "process.terminated", actor=actor, reason=(reason or "").strip()[:200] or None
        )
        record(self.session, actor, "process.terminate", "process", proc.uuid, proc.title)
        await self.session.flush()
        return proc

    # ───────────────────────────── 查询

    async def list(
        self,
        project_uuid: UUID,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 200,
    ) -> list[Process]:
        stmt = (
            select(Process).where(Process.project_uuid == project_uuid).order_by(Process.no.desc())
        )
        if status:
            stmt = stmt.where(Process.status == status)
        if q:
            stmt = stmt.where(Process.title.ilike(f"%{q.strip()}%"))
        return list(await self.session.scalars(stmt.limit(limit)))

    async def running_count(self, project_uuid: UUID, template_uuid: UUID | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(Process)
            .where(Process.project_uuid == project_uuid, Process.status == "running")
        )
        if template_uuid:
            stmt = stmt.where(Process.template_uuid == template_uuid)
        return int(await self.session.scalar(stmt) or 0)

    async def submissions(self, n: ProcessNode) -> list[Submission]:
        return list(
            await self.session.scalars(
                select(Submission).where(Submission.node_uuid == n.uuid).order_by(Submission.round)
            )
        )

    async def submissions_by_uuid(self, uuids: list[UUID]) -> dict[UUID, Submission]:
        if not uuids:
            return {}
        rows = await self.session.scalars(select(Submission).where(Submission.uuid.in_(uuids)))
        return {x.uuid: x for x in rows}

    async def last_activity(self, process_uuids: list[UUID]) -> dict[UUID, datetime]:
        """每条流程最近一条事件的时间（流程列表的「更新」）。"""
        if not process_uuids:
            return {}
        rows = await self.session.execute(
            select(ProcessEvent.process_uuid, func.max(ProcessEvent.created_at))
            .where(ProcessEvent.process_uuid.in_(process_uuids))
            .group_by(ProcessEvent.process_uuid)
        )
        return {u: t for u, t in rows.all()}

    async def reviews(self, n: ProcessNode) -> list[tuple[ReviewRound, list[Vote]]]:
        rounds = list(
            await self.session.scalars(
                select(ReviewRound).where(ReviewRound.node_uuid == n.uuid).order_by(ReviewRound.id)
            )
        )
        votes: dict[UUID, list[Vote]] = defaultdict(list)
        if rounds:
            for v in await self.session.scalars(
                select(Vote).where(Vote.review_uuid.in_([r.uuid for r in rounds])).order_by(Vote.id)
            ):
                votes[v.review_uuid].append(v)
        return [(r, votes[r.uuid]) for r in rounds]

    async def events(
        self,
        proc: Process,
        *,
        after: int = 0,
        node_id: str | None = None,
        types: list[str] | None = None,
        limit: int = 500,
    ) -> list[ProcessEvent]:
        stmt = select(ProcessEvent).where(
            ProcessEvent.process_uuid == proc.uuid, ProcessEvent.seq > after
        )
        if node_id:
            stmt = stmt.where(ProcessEvent.node_id == node_id)
        if types:
            stmt = stmt.where(ProcessEvent.type.in_(types))
        return list(await self.session.scalars(stmt.order_by(ProcessEvent.seq).limit(limit)))

    async def open_reviews_for(
        self, uid: UUID, *, project_uuid: UUID | None = None
    ) -> list[tuple[Process, ProcessNode, ReviewRound]]:
        """我在名单里、还没表态的评审（给「等你」）。"""
        voted = select(Vote.review_uuid).where(Vote.reviewer == uid)
        stmt = (
            select(Process, ProcessNode, ReviewRound)
            .join(ProcessNode, ProcessNode.process_uuid == Process.uuid)
            .join(ReviewRound, ReviewRound.node_uuid == ProcessNode.uuid)
            .where(
                Process.status == "running",
                ReviewRound.status == "open",
                type_coerce(ReviewRound.reviewers, JSONB).contains([str(uid)]),
                ReviewRound.uuid.not_in(voted),
            )
            .order_by(ReviewRound.id)
        )
        if project_uuid:
            stmt = stmt.where(Process.project_uuid == project_uuid)
        return [(p, n, r) for p, n, r in (await self.session.execute(stmt)).all()]

    async def node_context(self, node_uuid: UUID) -> NodeContext:
        """节点协作投递时要的上下文。数字员工还没锁（第一次开工时团队没有可用的数字员工）就现在补锁。"""
        proc, n = await self.node_by_uuid(node_uuid)
        if n.atlas_agent_id is None and n.status == "working":
            table = await self.roles(proc)
            agent = table.agents.get(n.exec_role)
            if agent is not None:
                n.atlas_agent_id = agent[1]
                await self.session.flush()
        return NodeContext(proc, n)

    async def agent_stats(self, team_uuid: UUID) -> dict[str, dict[str, Any]]:
        """按数字员工汇总：正在做的节点数、完成的节点数、平均几轮通过。"""
        rows = (
            await self.session.execute(
                select(ProcessNode.atlas_agent_id, ProcessNode.status, ProcessNode.round)
                .join(Process, Process.uuid == ProcessNode.process_uuid)
                .where(Process.team_uuid == team_uuid, ProcessNode.atlas_agent_id.is_not(None))
            )
        ).all()
        out: dict[str, dict[str, Any]] = {}
        for agent, status, rnd in rows:
            st = out.setdefault(agent, {"working": 0, "passed": 0, "rounds": 0})
            if status in ("working", "exit_review"):
                st["working"] += 1
            elif status in ("passed", "exited"):
                st["passed"] += 1
                st["rounds"] += rnd
        for st in out.values():
            st["avg_rounds"] = round(st.pop("rounds") / st["passed"], 1) if st["passed"] else None
        return out


def _uids(uids: list[UUID]) -> list[str]:
    return [str(u) for u in uids]


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
