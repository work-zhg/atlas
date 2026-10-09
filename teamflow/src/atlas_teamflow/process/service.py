"""流程运行（系统设计 §8.2、§9、§12）。

节点状态机：
  待开始 ──依赖全部通过──▶ 人机协同中 ──提交准出──▶ 待准出 ──规则满足──▶ 待准入 ──规则满足──▶ 已通过
                              ▲  驳回                 │                    │
                              └───────────────────────┴────────────────────┘
  打回到更早节点：目标节点回到人机协同中；它的下游里已开工 / 已通过的节点 → 已打回，
  等目标重新通过后再开工。

★ 并发：所有改变状态的操作先锁流程行（SELECT … FOR UPDATE），同一流程的推进串行。
★ 锁定：发起锁模板版本；节点首次开工锁文件模板版本与执行 Agent（打回重开不重新锁定）；
  进入评审时锁评审人名单。
★ 无超时：协同与评审都没有等待上限（atlas-no-timeouts 原则）。
★ 调 Atlas / Git 的慢操作不在流程锁里做：状态迁移只把「要发给 Agent 的指令」排队，
  事务提交后由 Agent 监督器投递（agents/supervisor.py）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record
from ..db.models import (
    ArtifactVersion,
    FileTemplate,
    FileTemplateVersion,
    FlowTemplate,
    FlowTemplateVersion,
    NodeMessage,
    Process,
    ProcessEvent,
    ProcessNode,
    Project,
    ReviewRound,
    RoleAssignment,
    TeamAgent,
    TfUser,
    Vote,
)
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..identity import LEVEL_RANK
from ..teams.service import TeamService
from ..templates import structure
from .bus import nudge_after_commit, wake_after_commit
from .git_store import GitStore, safe_segment

__all__ = ["ProcessService", "build_kickoff"]

ACTIVE = ("working", "exit_review", "admit_review")


def _text(value: str | None, label: str, max_len: int) -> str:
    v = (value or "").strip()
    if not v:
        raise Invalid("VALIDATION_FAILED", f"请填写{label}")
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符")
    return v


def build_kickoff(
    *,
    process: Process,
    node_def: dict[str, Any],
    file_template: tuple[str, int, str] | None,
    inputs: list[tuple[str, int, str]],
) -> str:
    """开工上下文（系统设计 §10.1）。inputs: [(上游产物名, 版本号, 内容)]。"""
    parts = [
        f"你是流程「{process.title}」中节点「{node_def['name']}」的执行者，"
        f"需要产出「{node_def['output_name']}」。",
        f"需求：{process.requirement}",
    ]
    if file_template:
        name, ver, content = file_template
        parts.append(f'<file-template name="{name}" version="{ver}">\n{content}\n</file-template>')
    for name, ver, content in inputs:
        parts.append(f'<input name="{name}" version="{ver}">\n{content}\n</input>')
    parts.append(
        "请先澄清不清楚的地方，再按文件模板产出，并对照模板自查。\n"
        "★ 交付产物时，把产物全文（Markdown）放在 <artifact> 与 </artifact> 之间，"
        "每次修改都输出完整全文；标记之外可以写说明。"
    )
    return "\n\n".join(parts)


class ProcessService:
    def __init__(self, teams: TeamService, git: GitStore) -> None:
        self.teams = teams
        self.session: AsyncSession = teams.session
        self.p = teams.p
        self.git = git

    @classmethod
    def system(cls, session: AsyncSession, git: GitStore) -> ProcessService:
        """Agent 监督器用的系统身份：只做状态写入（提交 Agent 产物、记事件），不做权限判断。"""
        self = cls.__new__(cls)
        self.teams = None  # type: ignore[assignment]
        self.session, self.p, self.git = session, None, git  # type: ignore[assignment]
        return self

    @property
    def actor(self) -> UUID | None:
        return self.p.uuid if self.p is not None else None

    # ───────────────────────────── 读取与权限

    async def _project(self, project_uuid: UUID) -> Project:
        pr = (
            await self.session.execute(select(Project).where(Project.uuid == project_uuid))
        ).scalar_one_or_none()
        if pr is None:
            raise NotFound("PROJECT_NOT_FOUND", "项目不存在")
        await self.teams.visible(pr.team_uuid)
        return pr

    async def get(self, uuid: UUID, *, lock: bool = False) -> Process:
        stmt = select(Process).where(Process.uuid == uuid)
        if lock:
            stmt = stmt.with_for_update()
        proc = (await self.session.execute(stmt)).scalar_one_or_none()
        if proc is None:
            raise NotFound("PROCESS_NOT_FOUND", "流程不存在")
        await self.teams.visible(proc.team_uuid)
        return proc

    async def _member(self, team_uuid: UUID, op: str) -> None:
        """流程动作 = 操作码 ∧ 团队读写（权限设计 §08）。"""
        self.p.require(op)
        if LEVEL_RANK[await self.teams.level(team_uuid)] < LEVEL_RANK["WRITE"]:
            raise Forbidden("TEAM_MEMBER_REQUIRED", "只有团队成员可以处理流程")

    async def definition(self, proc: Process) -> dict[str, Any]:
        v = (
            await self.session.execute(
                select(FlowTemplateVersion).where(
                    FlowTemplateVersion.uuid == proc.template_version_uuid
                )
            )
        ).scalar_one()
        return v.definition

    async def version_label(self, proc: Process) -> str:
        v = (
            await self.session.execute(
                select(FlowTemplateVersion.major, FlowTemplateVersion.minor).where(
                    FlowTemplateVersion.uuid == proc.template_version_uuid
                )
            )
        ).one()
        return f"v{v[0]}.{v[1]}"

    async def nodes(self, proc: Process) -> dict[str, ProcessNode]:
        rows = await self.session.scalars(
            select(ProcessNode).where(ProcessNode.process_uuid == proc.uuid)
        )
        return {n.node_id: n for n in rows}

    async def node(self, proc: Process, node_id: str) -> ProcessNode:
        n = (
            await self.session.execute(
                select(ProcessNode).where(
                    ProcessNode.process_uuid == proc.uuid, ProcessNode.node_id == node_id
                )
            )
        ).scalar_one_or_none()
        if n is None:
            raise NotFound("NODE_NOT_FOUND", "节点不存在")
        return n

    async def assignees(self, project_uuid: UUID, role: str) -> tuple[list[UUID], list[UUID]]:
        """项目当前分配：(人, 团队 Agent)。"""
        rows = list(
            await self.session.scalars(
                select(RoleAssignment)
                .where(
                    RoleAssignment.project_uuid == project_uuid, RoleAssignment.role_name == role
                )
                .order_by(RoleAssignment.sort, RoleAssignment.id)
            )
        )
        return (
            [r.assignee_uuid for r in rows if r.assignee_type == "user"],
            [r.assignee_uuid for r in rows if r.assignee_type == "agent"],
        )

    async def _require_executor(self, proc: Process, node: ProcessNode) -> None:
        await self._member(proc.team_uuid, "process:handle")
        users, _ = await self.assignees(proc.project_uuid, node.exec_role)
        if self.p.uuid not in users:
            raise Forbidden("NOT_EXECUTOR", f"只有执行角色「{node.exec_role}」的成员可以操作该节点")

    def _require_running(self, proc: Process) -> None:
        if proc.status != "running":
            raise Conflict("PROCESS_NOT_RUNNING", "流程已结束")

    # ───────────────────────────── 事件

    def emit(self, proc: Process, type_: str, node_id: str | None = None, **data: Any) -> None:
        proc.event_seq += 1
        self.session.add(
            ProcessEvent(
                process_uuid=proc.uuid,
                seq=proc.event_seq,
                type=type_,
                node_id=node_id,
                actor_uuid=self.actor,
                data={k: (str(v) if isinstance(v, UUID) else v) for k, v in data.items()},
            )
        )
        nudge_after_commit(self.session.sync_session, proc.uuid)

    def _queue(self, node: ProcessNode, role: str, content: str) -> NodeMessage:
        """排一条要发给 Agent 的消息（事务提交后由监督器投递）。"""
        m = NodeMessage(
            node_uuid=node.uuid,
            role=role,
            author_uuid=self.actor if role == "user" else None,
            content=content,
            status="queued" if node.agent_uuid else "done",
            round=node.round,
        )
        self.session.add(m)
        if node.agent_uuid:
            wake_after_commit(self.session.sync_session, node.uuid)
        return m

    # ───────────────────────────── 发起

    async def start(self, project_uuid: UUID, title: str, requirement: str) -> Process:
        pr = await self._project(project_uuid)
        await self._member(pr.team_uuid, "process:start")
        if pr.status != "active":
            raise Conflict("PROJECT_ARCHIVED", "项目已归档，不能发起流程")
        tpl = (
            await self.session.execute(
                select(FlowTemplate).where(FlowTemplate.uuid == pr.flow_template_uuid)
            )
        ).scalar_one_or_none()
        if tpl is None or tpl.current_version_uuid is None:
            raise Invalid("TEMPLATE_NOT_PUBLISHED", "项目还没有绑定已发布的流程模板")
        # 同一项目的编号在项目行锁内分配
        await self.session.execute(
            select(Project.id).where(Project.uuid == pr.uuid).with_for_update()
        )
        no = (
            int(
                await self.session.scalar(
                    select(func.coalesce(func.max(Process.no), 0)).where(
                        Process.project_uuid == pr.uuid
                    )
                )
                or 0
            )
            + 1
        )
        proc = Process(
            team_uuid=pr.team_uuid,
            project_uuid=pr.uuid,
            no=no,
            title=_text(title, "流程名称", 100),
            requirement=_text(requirement, "需求", 5000),
            template_uuid=tpl.uuid,
            template_version_uuid=tpl.current_version_uuid,
            started_by=self.p.uuid,
        )
        self.session.add(proc)
        await self.session.flush()
        definition = await self.definition(proc)
        for nid in structure.flow_node_ids(definition["flow"]):
            nd = definition["nodes"][nid]
            self.session.add(
                ProcessNode(
                    process_uuid=proc.uuid,
                    node_id=nid,
                    name=nd["name"],
                    exec_role=nd["exec_role"],
                )
            )
        await self.session.flush()
        self.emit(proc, "process.started", title=proc.title, version=await self.version_label(proc))
        record(self.session, self.p.uuid, "process.start", "process", proc.uuid, proc.title)
        await self._advance(proc)
        await self.session.flush()
        return proc

    # ───────────────────────────── 调度

    async def _advance(self, proc: Process) -> None:
        """开工所有依赖已满足的节点；全部通过则流程完成。"""
        definition = await self.definition(proc)
        deps = structure.derive_deps(definition["flow"])
        nodes = await self.nodes(proc)
        for nid, ups in deps.items():
            n = nodes[nid]
            if n.status in ("pending", "returned") and all(
                nodes[u].status == "passed" for u in ups
            ):
                await self._start_node(proc, n, definition, deps, nodes)
        if all(n.status == "passed" for n in nodes.values()):
            proc.status = "completed"
            proc.finished_at = utcnow()
            self.emit(proc, "process.completed")

    async def _start_node(
        self,
        proc: Process,
        n: ProcessNode,
        definition: dict[str, Any],
        deps: dict[str, list[str]],
        nodes: dict[str, ProcessNode],
    ) -> None:
        nd = definition["nodes"][n.node_id]
        restart = n.started_at is not None
        n.status = "working"
        n.round += 1
        n.notice = None
        if not restart:
            n.started_at = utcnow()
            # 锁定文件模板版本
            ft_id = nd.get("artifact_file_template_id")
            if ft_id:
                ft = (
                    await self.session.execute(
                        select(FileTemplate).where(FileTemplate.uuid == UUID(ft_id))
                    )
                ).scalar_one_or_none()
                n.file_template_version_uuid = ft.current_version_uuid if ft else None
            # 锁定执行 Agent
            _, agents = await self.assignees(proc.project_uuid, n.exec_role)
            ta = None
            if agents:
                ta = (
                    await self.session.execute(select(TeamAgent).where(TeamAgent.uuid == agents[0]))
                ).scalar_one_or_none()
            if ta is None:
                n.notice = (
                    f"执行角色「{n.exec_role}」没有分配 Agent："
                    "可由人直接提交产物，或请团队管理员分配"
                )
            elif not ta.available:
                n.notice = f"执行 Agent「{ta.name}」不可用，请团队管理员更换分配"
            else:
                n.agent_uuid, n.atlas_agent_id = ta.uuid, ta.atlas_agent_id
        await self.session.flush()
        inputs = await self._inputs(definition, deps, nodes, n)
        if not restart:
            self._queue(
                n,
                "system",
                build_kickoff(
                    process=proc,
                    node_def=nd,
                    file_template=await self._file_template(n),
                    inputs=inputs,
                ),
            )
        else:
            text = "上游产物已更新，请基于新的输入修订产物，并输出完整全文。"
            for name, ver, content in inputs:
                text += f'\n\n<input name="{name}" version="{ver}">\n{content}\n</input>'
            self._queue(n, "system", text)
        self.emit(proc, "node.started", n.node_id, round=n.round, notice=n.notice)

    async def _file_template(self, n: ProcessNode) -> tuple[str, int, str] | None:
        if not n.file_template_version_uuid:
            return None
        row = (
            await self.session.execute(
                select(
                    FileTemplate.name, FileTemplateVersion.version_no, FileTemplateVersion.content
                )
                .join(FileTemplate, FileTemplate.uuid == FileTemplateVersion.template_uuid)
                .where(FileTemplateVersion.uuid == n.file_template_version_uuid)
            )
        ).one_or_none()
        return (row[0], row[1], row[2]) if row else None

    async def _inputs(
        self,
        definition: dict[str, Any],
        deps: dict[str, list[str]],
        nodes: dict[str, ProcessNode],
        n: ProcessNode,
    ) -> list[tuple[str, int, str]]:
        out = []
        for up in deps.get(n.node_id, []):
            un = nodes[up]
            if un.current_artifact_uuid:
                a = await self._artifact(un.current_artifact_uuid)
                out.append((definition["nodes"][up]["output_name"], a.version_no, a.content))
        return out

    async def _artifact(self, uuid: UUID) -> ArtifactVersion:
        return (
            await self.session.execute(select(ArtifactVersion).where(ArtifactVersion.uuid == uuid))
        ).scalar_one()

    # ───────────────────────────── 人机协同

    async def send_message(self, process_uuid: UUID, node_id: str, text: str) -> NodeMessage:
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        n = await self.node(proc, node_id)
        await self._require_executor(proc, n)
        if n.status != "working":
            raise Conflict("NODE_NOT_WORKING", "节点不在人机协同中")
        if not n.agent_uuid:
            raise Conflict("NO_AGENT", "该节点没有可用的执行 Agent，请直接提交产物")
        m = self._queue(n, "user", _text(text, "指令", 20000))
        self.emit(proc, "message.sent", n.node_id)
        await self.session.flush()
        return m

    async def artifact_path(self, proc: Process, n: ProcessNode, output_name: str) -> str:
        definition = await self.definition(proc)
        idx = structure.flow_node_ids(definition["flow"]).index(n.node_id) + 1
        return (
            f"{proc.no:04d}-{safe_segment(proc.title)}/"
            f"{idx:02d}-{safe_segment(n.name)}/{safe_segment(output_name)}.md"
        )

    async def commit_artifact(
        self,
        proc: Process,
        n: ProcessNode,
        content: str,
        *,
        source: str,
        author: UUID | None,
        note: str | None = None,
    ) -> ArtifactVersion:
        """提交一个产物版本（Git commit + 库内缓存）。调用方已持有流程锁。"""
        definition = await self.definition(proc)
        nd = definition["nodes"][n.node_id]
        path = await self.artifact_path(proc, n, nd["output_name"])
        no = (
            int(
                await self.session.scalar(
                    select(func.coalesce(func.max(ArtifactVersion.version_no), 0)).where(
                        ArtifactVersion.node_uuid == n.uuid
                    )
                )
                or 0
            )
            + 1
        )
        who = "Agent" if source == "agent" else "人工"
        if author:
            name = await self.session.scalar(select(TfUser.name).where(TfUser.uuid == author))
            who = f"{who}（{name}）"
        message = (
            f"{proc.title} · {n.name} · {nd['output_name']} v{no}\n\n"
            f"{note or ''}\n\n"
            f"Process: {proc.uuid}\nNode: {n.node_id}\nRound: {n.round}\nBy: {who}\n"
        )
        sha = await self.git.commit(proc.team_uuid, proc.project_uuid, path, content, message)
        a = ArtifactVersion(
            node_uuid=n.uuid,
            version_no=no,
            round=n.round,
            path=path,
            commit_sha=sha,
            content=content,
            source=source,
            created_by=author,
            note=(note or None) and note[:200],
        )
        self.session.add(a)
        await self.session.flush()
        n.current_artifact_uuid = a.uuid
        self.emit(proc, "artifact.committed", n.node_id, version=no, source=source, commit=sha[:10])
        return a

    async def submit_artifact(
        self, process_uuid: UUID, node_id: str, content: str, note: str | None
    ) -> ArtifactVersion:
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        n = await self.node(proc, node_id)
        await self._require_executor(proc, n)
        if n.status != "working":
            raise Conflict("NODE_NOT_WORKING", "节点不在人机协同中")
        body = (content or "").strip()
        if not body:
            raise Invalid("VALIDATION_FAILED", "产物内容不能为空")
        if len(body.encode()) > 512 * 1024:
            raise Invalid("VALIDATION_FAILED", "产物超过 512 KB")
        return await self.commit_artifact(
            proc, n, body, source="user", author=self.p.uuid, note=note
        )

    # ───────────────────────────── 评审

    async def _open_round(
        self, proc: Process, n: ProcessNode, stage: str, review: dict[str, Any]
    ) -> ReviewRound:
        users, _ = await self.assignees(proc.project_uuid, review["role"])
        rr = ReviewRound(
            node_uuid=n.uuid,
            stage=stage,
            round=n.round,
            role=review["role"],
            rule=review["rule"],
            reviewers=[str(u) for u in users],
            artifact_uuid=n.current_artifact_uuid,  # type: ignore[arg-type]
        )
        self.session.add(rr)
        n.status = "exit_review" if stage == "exit" else "admit_review"
        n.notice = (
            None
            if users
            else f"评审角色「{review['role']}」没有分配人：请团队管理员分配后，由执行人重新提交评审"
        )
        await self.session.flush()
        self.emit(
            proc,
            "review.opened",
            n.node_id,
            stage=stage,
            role=review["role"],
            rule=review["rule"],
            reviewers=len(users),
        )
        return rr

    async def open_round(self, n: ProcessNode) -> ReviewRound | None:
        return (
            await self.session.execute(
                select(ReviewRound).where(
                    ReviewRound.node_uuid == n.uuid, ReviewRound.status == "open"
                )
            )
        ).scalar_one_or_none()

    async def submit_review(self, process_uuid: UUID, node_id: str) -> ProcessNode:
        """提交准出评审；评审人名单为空的待评审轮次可以重新提交以刷新名单。"""
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        n = await self.node(proc, node_id)
        await self._require_executor(proc, n)
        nd = (await self.definition(proc))["nodes"][n.node_id]
        if n.status in ("exit_review", "admit_review"):
            rr = await self.open_round(n)
            if rr is None or rr.reviewers:
                raise Conflict("ALREADY_IN_REVIEW", "节点已在评审中")
            rr.status, rr.closed_at = "cancelled", utcnow()
            stage = rr.stage
            await self._open_round(
                proc, n, stage, nd["exit_review" if stage == "exit" else "admit_review"]
            )
            return n
        if n.status != "working":
            raise Conflict("NODE_NOT_WORKING", "节点不在人机协同中")
        if not n.current_artifact_uuid:
            raise Conflict("NO_ARTIFACT", "还没有产物，不能提交评审")
        a = await self._artifact(n.current_artifact_uuid)
        if a.round != n.round:
            # 驳回 / 打回后回到协同中：必须有本轮的新版本（内容不变也可人工重新提交一次）
            raise Conflict("ARTIFACT_NOT_REVISED", "本轮还没有新的产物版本")
        await self._open_round(proc, n, "exit", nd["exit_review"])
        return n

    async def vote(
        self,
        process_uuid: UUID,
        node_id: str,
        decision: str,
        comment: str | None,
        return_to: str | None,
    ) -> ProcessNode:
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        await self._member(proc.team_uuid, "process:review")
        n = await self.node(proc, node_id)
        rr = await self.open_round(n)
        if rr is None:
            raise Conflict("NO_OPEN_REVIEW", "节点不在评审中")
        if str(self.p.uuid) not in rr.reviewers:
            raise Forbidden("NOT_REVIEWER", "你不在本轮评审名单中")
        if await self.session.scalar(
            select(Vote.id).where(Vote.review_uuid == rr.uuid, Vote.user_uuid == self.p.uuid)
        ):
            raise Conflict("ALREADY_VOTED", "你已经投过票")
        if decision not in ("approve", "reject"):
            raise Invalid("VALIDATION_FAILED", "decision 只能是 approve 或 reject")
        definition = await self.definition(proc)
        deps = structure.derive_deps(definition["flow"])
        if decision == "reject":
            comment = _text(comment, "驳回意见", 5000)
            if return_to and return_to not in _ancestors(deps, n.node_id):
                raise Invalid("INVALID_RETURN_TARGET", "只能打回到本节点的上游节点")
        self.session.add(
            Vote(
                review_uuid=rr.uuid,
                user_uuid=self.p.uuid,
                decision=decision,
                comment=(comment or "").strip() or None,
                return_to=return_to if decision == "reject" else None,
            )
        )
        self.emit(proc, "vote.cast", n.node_id, stage=rr.stage, decision=decision)
        await self.session.flush()
        if decision == "reject":
            rr.status, rr.closed_at = "rejected", utcnow()
            self.emit(proc, "review.rejected", n.node_id, stage=rr.stage, return_to=return_to)
            if return_to:
                await self._send_back(proc, definition, deps, n, return_to, comment or "")
            else:
                n.status = "working"
                n.round += 1
                n.notice = None
                self._queue(
                    n,
                    "system",
                    f"{'准出' if rr.stage == 'exit' else '准入'}评审驳回，"
                    f"请按意见修改产物并输出完整全文：\n\n{comment}",
                )
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
            rr.status, rr.closed_at = "passed", utcnow()
            self.emit(proc, "review.passed", n.node_id, stage=rr.stage)
            admit = definition["nodes"][n.node_id].get("admit_review")
            if rr.stage == "exit" and admit:
                await self._open_round(proc, n, "admit", admit)
            else:
                n.status, n.passed_at, n.notice = "passed", utcnow(), None
                self.emit(proc, "node.passed", n.node_id)
                await self.session.flush()
                await self._advance(proc)
        await self.session.flush()
        return n

    async def _send_back(
        self,
        proc: Process,
        definition: dict[str, Any],
        deps: dict[str, list[str]],
        n: ProcessNode,
        target_id: str,
        comment: str,
    ) -> None:
        """打回：目标节点回到人机协同中；它下游已开工 / 已通过的节点 → 已打回。"""
        nodes = await self.nodes(proc)
        target = nodes[target_id]
        for did in _descendants(deps, target_id):
            d = nodes[did]
            if d.status != "pending":
                rr = await self.open_round(d)
                if rr:
                    rr.status, rr.closed_at = "cancelled", utcnow()
                d.status = "returned"
                d.notice = f"上游「{target.name}」被打回修改，重新通过后本节点将重新开工"
        target.status = "working"
        target.round += 1
        target.passed_at = None
        target.notice = None
        self._queue(
            target,
            "system",
            f"下游节点「{n.name}」评审时打回到本节点，请按意见修改产物并输出完整全文：\n\n{comment}",
        )
        self.emit(proc, "node.returned", target_id, by=n.node_id)

    # ───────────────────────────── 终止

    async def terminate(self, process_uuid: UUID, reason: str | None) -> Process:
        proc = await self.get(process_uuid, lock=True)
        self._require_running(proc)
        self.p.require("process:terminate")
        owner = LEVEL_RANK[await self.teams.level(proc.team_uuid)] >= LEVEL_RANK["OWNER"]
        if proc.started_by != self.p.uuid and not owner:
            raise Forbidden(
                "TERMINATE_DENIED", "只能终止自己发起的流程（团队管理员可终止任一流程）"
            )
        proc.status, proc.finished_at = "terminated", utcnow()
        for n in (await self.nodes(proc)).values():
            rr = await self.open_round(n)
            if rr:
                rr.status, rr.closed_at = "cancelled", utcnow()
        self.emit(proc, "process.terminated", reason=(reason or "").strip()[:200] or None)
        record(self.session, self.p.uuid, "process.terminate", "process", proc.uuid, proc.title)
        await self.session.flush()
        return proc

    # ───────────────────────────── 查询

    async def list(
        self, project_uuid: UUID, *, status: str | None, mine: bool, q: str | None
    ) -> list[Process]:
        pr = await self._project(project_uuid)
        stmt = select(Process).where(Process.project_uuid == pr.uuid).order_by(Process.no.desc())
        if status:
            stmt = stmt.where(Process.status == status)
        if q:
            stmt = stmt.where(Process.title.ilike(f"%{q.strip()}%"))
        rows = list(await self.session.scalars(stmt.limit(200)))
        if mine:
            todo = {t["process_id"] for t in await self.todos(project_uuid=pr.uuid)}
            rows = [r for r in rows if str(r.uuid) in todo]
        return rows

    async def todos(self, *, project_uuid: UUID | None = None) -> list[dict[str, Any]]:
        """待我处理：我在执行角色中的协同中节点 + 我在名单里且未投票的评审（系统设计 §13）。"""
        me = self.p.uuid
        work = (
            select(Process, ProcessNode)
            .join(ProcessNode, ProcessNode.process_uuid == Process.uuid)
            .join(
                RoleAssignment,
                (RoleAssignment.project_uuid == Process.project_uuid)
                & (RoleAssignment.role_name == ProcessNode.exec_role)
                & (RoleAssignment.assignee_type == "user")
                & (RoleAssignment.assignee_uuid == me),
            )
            .where(Process.status == "running", ProcessNode.status == "working")
        )
        voted = select(Vote.review_uuid).where(Vote.user_uuid == me)
        review = (
            select(Process, ProcessNode, ReviewRound)
            .join(ProcessNode, ProcessNode.process_uuid == Process.uuid)
            .join(ReviewRound, ReviewRound.node_uuid == ProcessNode.uuid)
            .where(
                Process.status == "running",
                ReviewRound.status == "open",
                type_coerce(ReviewRound.reviewers, JSONB).contains([str(me)]),
                ReviewRound.uuid.not_in(voted),
            )
        )
        if project_uuid:
            work = work.where(Process.project_uuid == project_uuid)
            review = review.where(Process.project_uuid == project_uuid)
        out: list[dict[str, Any]] = []
        for proc, n in (await self.session.execute(work)).all():
            out.append(_todo(proc, n, "work", n.started_at))
        for proc, n, rr in (await self.session.execute(review)).all():
            out.append(_todo(proc, n, f"{rr.stage}_review", rr.opened_at))
        # 只保留我仍然看得见的团队
        visible: dict[UUID, bool] = {}
        result = []
        for t in out:
            tid = UUID(t["team_id"])
            if tid not in visible:
                visible[tid] = (
                    self.p.has("team:manage_all")
                    or LEVEL_RANK[await self.teams.level(tid)] >= LEVEL_RANK["WRITE"]
                )
            if visible[tid]:
                result.append(t)
        result.sort(key=lambda t: t["since"] or "", reverse=True)
        return result

    async def events(self, proc: Process, after: int, limit: int = 500) -> list[ProcessEvent]:
        return list(
            await self.session.scalars(
                select(ProcessEvent)
                .where(ProcessEvent.process_uuid == proc.uuid, ProcessEvent.seq > after)
                .order_by(ProcessEvent.seq)
                .limit(limit)
            )
        )

    async def messages(self, n: ProcessNode) -> list[NodeMessage]:
        return list(
            await self.session.scalars(
                select(NodeMessage).where(NodeMessage.node_uuid == n.uuid).order_by(NodeMessage.id)
            )
        )

    async def artifacts(self, n: ProcessNode) -> list[ArtifactVersion]:
        return list(
            await self.session.scalars(
                select(ArtifactVersion)
                .where(ArtifactVersion.node_uuid == n.uuid)
                .order_by(ArtifactVersion.version_no.desc())
            )
        )

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


def _todo(proc: Process, n: ProcessNode, kind: str, since: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "team_id": str(proc.team_uuid),
        "project_id": str(proc.project_uuid),
        "process_id": str(proc.uuid),
        "process_no": proc.no,
        "process_title": proc.title,
        "node_id": n.node_id,
        "node_name": n.name,
        "since": since.isoformat() if since else None,
    }


def _ancestors(deps: dict[str, list[str]], nid: str) -> set[str]:
    out: set[str] = set()
    stack = list(deps.get(nid, []))
    while stack:
        u = stack.pop()
        if u not in out:
            out.add(u)
            stack.extend(deps.get(u, []))
    return out


def _descendants(deps: dict[str, list[str]], nid: str) -> set[str]:
    down: dict[str, list[str]] = defaultdict(list)
    for d, ups in deps.items():
        for u in ups:
            down[u].append(d)
    out: set[str] = set()
    stack = list(down.get(nid, []))
    while stack:
        d = stack.pop()
        if d not in out:
            out.add(d)
            stack.extend(down.get(d, []))
    return out
