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
    GitAccount,
    NodeMessage,
    Process,
    ProcessEvent,
    ProcessNode,
    Project,
    ReviewRound,
    RoleAssignment,
    TeamAgent,
    Vote,
)
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..identity import LEVEL_RANK
from ..teams.service import TeamService
from ..templates import structure
from .bus import nudge_after_commit, wake_after_commit
from .repo_host import CommitInfo, RepoHost, RepoInfo, safe_segment
from .repository import ensure_project_repo

__all__ = ["ProcessService", "build_kickoff"]

ACTIVE = ("working", "exit_review", "admit_review")


def _text(value: str | None, label: str, max_len: int) -> str:
    v = (value or "").strip()
    if not v:
        raise Invalid("VALIDATION_FAILED", f"请填写{label}")
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符")
    return v


#: 提交说明的尾注：TeamFlow 据此识别 Agent 的提交与所属节点（产物仓库与 Agent 提交 · §5.2）
TRAILER_NODE = "TeamFlow-Node"


def commit_template(process: Process, node: ProcessNode, agent_name: str) -> str:
    return (
        f"{process.title} · {node.name} · 第 {node.round} 轮\n"
        "\n"
        "<一句话说明本次修改>\n"
        "\n"
        f"TeamFlow-Process: {process.uuid}\n"
        f"{TRAILER_NODE}: {node.node_id}\n"
        f"TeamFlow-Round: {node.round}\n"
        f"Agent: {agent_name}"
    )


def git_steps(repo: RepoInfo, node: ProcessNode, template: str) -> str:
    """在 Pod 里用 git CLI 提交产物的做法（会话 Pod 已挂好 git 凭据，见 Atlas git 凭据档案）。"""
    local = f"/repos/{repo.full_name.split('/')[-1]}"
    node_dir = (node.artifact_path or "").rsplit("/", 1)[0]
    return (
        "在命令行里操作产物仓库（git 凭据已由平台配置好，直接用 git；"
        "不要询问、打印或保存任何密码 / 令牌）：\n"
        f"1. 准备仓库：未 clone 过执行 `git clone {repo.clone_url} {local}`；"
        f"已 clone 过执行 `cd {local} && git pull --rebase`\n"
        "2. 读输入：`git show <commit>:<path>`（commit 与 path 见 <input>）\n"
        f"3. 把产物完整全文（Markdown）写入 `{node.artifact_path}`；"
        f"只修改 `{node_dir}/` 目录，不要改其他路径\n"
        f"4. 提交并推送：`git add -- '{node_dir}'`，再用下面的提交说明 `git commit -F -`，"
        "然后 `git push origin HEAD:master`；推送被拒绝时先 `git pull --rebase` 再推送\n"
        f"提交说明：\n```\n{template}\n```\n"
        "5. 推送成功后，在回复中给出 commit 号与修改说明。\n"
        "如果你不能执行命令，请直接说明，不要只在回复里贴产物全文 —— 没有提交就没有产物。"
    )


def build_kickoff(
    *,
    process: Process,
    node_def: dict[str, Any],
    node: ProcessNode,
    repo: RepoInfo,
    agent_name: str,
    file_template: tuple[str, int, str] | None,
    inputs: list[tuple[str, str, str]],
) -> str:
    """开工上下文（产物仓库与 Agent 提交 · §7）。inputs: [(上游产物名, 路径, commit)]。

    ★ 上游产物不再内联全文：给路径与 commit，由 Agent 用 git 读取（也避免上下文过长）。
    """
    repo_xml = [
        f'<repository provider="gitee" clone="{repo.clone_url}" branch="master" '
        f'web="{repo.web_url}">',
        f'  <output path="{node.artifact_path}" />',
        *(f'  <input name="{n}" path="{p}" commit="{c}" />' for n, p, c in inputs),
        "</repository>",
    ]
    parts = [
        f"你是流程「{process.title}」中节点「{node_def['name']}」的执行者，"
        f"需要产出「{node_def['output_name']}」。",
        "\n".join(repo_xml),
    ]
    if file_template:
        name, ver, content = file_template
        parts.append(f'<file-template name="{name}" version="{ver}">\n{content}\n</file-template>')
    if inputs:
        parts.append(
            "以上游产物为输入；执行人可能在对话中补充要求，以执行人的说明为准。"
            "先澄清不清楚的地方，再按文件模板产出，并对照模板自查。"
        )
    else:
        # 发起流程只填名称：这段上下文与执行人的第一条说明合成一条发出（见 _with_pending_kickoff）
        parts.append(
            "本节点没有上游输入，要做什么以本消息末尾执行人的说明为准。"
            "先澄清不清楚的地方，再按文件模板产出，并对照模板自查。"
        )
    parts.append(git_steps(repo, node, commit_template(process, node, agent_name)))
    return "\n\n".join(parts)


class ProcessService:
    def __init__(self, teams: TeamService, repo: RepoHost) -> None:
        self.teams = teams
        self.session: AsyncSession = teams.session
        self.p = teams.p
        self.repo = repo

    @classmethod
    def system(cls, session: AsyncSession, repo: RepoHost) -> ProcessService:
        """Agent 监督器 / 产物观察用的系统身份：只做状态写入（登记产物、记事件），不做权限判断。"""
        self = cls.__new__(cls)
        self.teams = None  # type: ignore[assignment]
        self.session, self.p, self.repo = session, None, repo  # type: ignore[assignment]
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

    async def start(self, project_uuid: UUID, title: str) -> Process:
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
        # 产物仓库须就绪（老项目在这里补建）：Agent 要往里提交
        await ensure_project_repo(self.session, self.repo, pr)
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
        """推进：上游都已准出的节点发起准入（没配准入则直接开工）；全部通过则流程完成。

        准入由下游节点发起、审核上游产物（下阶段准入）：
        - 上游准出通过且有下游 → exited（待下游准入）；
        - 下游的上游全部 exited / passed → 下游配了准入就进入 admit_review，否则直接开工；
        - 下游开工后，上游的全部下游都开工了，上游才通过（passed）。
        """
        definition = await self.definition(proc)
        deps = structure.derive_deps(definition["flow"])
        nodes = await self.nodes(proc)
        for nid, ups in deps.items():
            n = nodes[nid]
            if n.status not in ("pending", "returned"):
                continue
            if not all(nodes[u].status in ("exited", "passed") for u in ups):
                continue
            admit = definition["nodes"][nid].get("admit_review")
            if admit and any(nodes[u].status == "exited" for u in ups):
                await self._open_round(proc, n, "admit", admit)
            else:
                await self._start_node(proc, n, definition, deps, nodes)
                self._settle_upstreams(proc, deps, nodes, nid)
        if all(n.status == "passed" for n in nodes.values()):
            proc.status = "completed"
            proc.finished_at = utcnow()
            self.emit(proc, "process.completed")

    def _settle_upstreams(
        self,
        proc: Process,
        deps: dict[str, list[str]],
        nodes: dict[str, ProcessNode],
        nid: str,
    ) -> None:
        """nid 开工后：其上游中「全部下游都已开工」的待下游准入节点 → 通过。"""
        started = ("working", "exit_review", "exited", "passed")
        for u in deps.get(nid, []):
            up = nodes[u]
            if up.status != "exited":
                continue
            downs = [d for d, ups in deps.items() if u in ups]
            if all(nodes[d].status in started for d in downs):
                up.status, up.passed_at, up.notice = "passed", utcnow(), None
                self.emit(proc, "node.passed", u)

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
            n.artifact_path = await self.artifact_path(proc, n, nd["output_name"])
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
                n.notice = f"执行角色「{n.exec_role}」没有分配 Agent，请团队管理员分配"
            elif not ta.available:
                n.notice = f"执行 Agent「{ta.name}」不可用，请团队管理员更换分配"
            else:
                n.agent_uuid, n.atlas_agent_id = ta.uuid, ta.atlas_agent_id
                n.notice = await self._shell_capability_notice(ta)
        await self.session.flush()
        inputs = await self._inputs(definition, deps, nodes, n)
        repo = await self._repo_info(proc)
        agent_name = await self._agent_name(n)
        if not restart and not inputs:
            # 没有上游输入：不空跑 Agent。等执行人第一次说明要做什么时，
            # 开工上下文与说明合成一条投递（_with_pending_kickoff）
            self.emit(proc, "node.started", n.node_id, round=n.round, notice=n.notice)
            return
        if not restart:
            text = await self._kickoff(proc, n, inputs)
        else:
            text = "上游产物已更新，请基于新的输入修订产物。\n\n" + "\n".join(
                f'<input name="{name}" path="{path}" commit="{sha}" />'
                for name, path, sha in inputs
            )
            text += "\n\n" + git_steps(repo, n, commit_template(proc, n, agent_name))
        self._queue(n, "system", text)
        self.emit(proc, "node.started", n.node_id, round=n.round, notice=n.notice)

    async def _kickoff(
        self, proc: Process, n: ProcessNode, inputs: list[tuple[str, str, str]]
    ) -> str:
        definition = await self.definition(proc)
        return build_kickoff(
            process=proc,
            node_def=definition["nodes"][n.node_id],
            node=n,
            repo=await self._repo_info(proc),
            agent_name=await self._agent_name(n),
            file_template=await self._file_template(n),
            inputs=inputs,
        )

    async def _with_pending_kickoff(
        self, proc: Process, n: ProcessNode, text: str, who: str
    ) -> str:
        """节点还没给 Agent 发过任何东西（没有上游、开工时没有投递）→ 把开工上下文拼在前面。"""
        if n.atlas_thread_id or not n.agent_uuid:
            return text
        sent = await self.session.scalar(
            select(NodeMessage.id).where(NodeMessage.node_uuid == n.uuid).limit(1)
        )
        if sent:
            return text
        return f"{await self._kickoff(proc, n, [])}\n\n{who}：\n{text}"

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
    ) -> list[tuple[str, str, str]]:
        """上游产物：(产物名, 路径, 当前登记版本的 commit)。"""
        out = []
        for up in deps.get(n.node_id, []):
            un = nodes[up]
            if un.current_artifact_uuid:
                a = await self._artifact(un.current_artifact_uuid)
                out.append((definition["nodes"][up]["output_name"], a.path, a.commit_sha))
        return out

    async def _repo_info(self, proc: Process) -> RepoInfo:
        name = await self.session.scalar(
            select(Project.repo_name).where(Project.uuid == proc.project_uuid)
        )
        return self.repo.info(name or self.repo.repo_name(proc.project_uuid))

    async def _agent_name(self, n: ProcessNode) -> str:
        if not n.agent_uuid:
            return "Agent"
        return (
            await self.session.scalar(select(TeamAgent.name).where(TeamAgent.uuid == n.agent_uuid))
            or "Agent"
        )

    async def _shell_capability_notice(self, ta: TeamAgent) -> str | None:
        """产物要由 Agent 在 Pod 里用 git 提交：执行 Agent 必须能执行命令
        （ACP 类 Agent，或带 ACP 子智能体的原生 Agent）。查不到时不拦，只在运行中暴露。"""
        atlas = getattr(self.teams, "atlas", None) if self.teams else None
        if atlas is None:
            return None
        try:
            a = await atlas.get_agent(ta.atlas_agent_id)
        except Exception:
            return None
        if not a:
            return f"执行 Agent「{ta.name}」在 Atlas 中不存在，请团队管理员更换分配"
        if a.get("kind") == "acp" or any(s.get("kind") == "acp" for s in a.get("subagents") or []):
            return None
        return (
            f"执行 Agent「{ta.name}」不能执行命令（需要 ACP 类 Agent，或带 ACP 子智能体），"
            "无法向产物仓库提交，请到 Atlas 配置或更换分配"
        )

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
            raise Conflict("NO_AGENT", "该节点没有可用的执行 Agent，请团队管理员分配")
        body = _text(text, "指令", 20000)
        m = self._queue(n, "user", await self._with_pending_kickoff(proc, n, body, "执行人的说明"))
        self.emit(proc, "message.sent", n.node_id)
        await self.session.flush()
        return m

    @staticmethod
    def process_dir(proc: Process) -> str:
        """本流程在产物仓库里的目录（各节点产物都在它下面；目录预览只开放这里）。"""
        return f"{proc.no:04d}-{safe_segment(proc.title)}/"

    async def artifact_path(self, proc: Process, n: ProcessNode, output_name: str) -> str:
        definition = await self.definition(proc)
        idx = structure.flow_node_ids(definition["flow"]).index(n.node_id) + 1
        node_dir = f"{idx:02d}-{safe_segment(n.name)}"
        return f"{self.process_dir(proc)}{node_dir}/{safe_segment(output_name)}.md"

    async def _latest_commit(self, proc: Process, n: ProcessNode) -> tuple[CommitInfo, str]:
        """产物路径上最新的提交与该提交的产物全文（访问托管平台：调用方不要持有流程锁）。"""
        repo = (await self._repo_info(proc)).full_name.split("/")[-1]
        path = n.artifact_path or ""
        commits = await self.repo.commits(repo, path=path, limit=1)
        if not commits:
            raise Conflict(
                "NO_ARTIFACT", f"产物路径还没有提交：{path}（让 Agent 按要求提交并推送）"
            )
        content = await self.repo.read_file(repo, path, commits[0].sha)
        if content is None:
            raise Conflict("NO_ARTIFACT", f"最新提交里没有产物文件：{path}")
        return commits[0], content

    async def _source(self, n: ProcessNode, commit: CommitInfo) -> tuple[str, UUID | None]:
        """来源：带本节点尾注 → agent；作者是已绑定账号的成员 → user；否则 unknown。"""
        trailer = f"{TRAILER_NODE}: {n.node_id}"
        if any(line.strip() == trailer for line in commit.message.splitlines()):
            return "agent", None
        if commit.author_login:
            user = await self.session.scalar(
                select(GitAccount.user_uuid).where(GitAccount.login == commit.author_login)
            )
            if user:
                return "user", user
        return "unknown", None

    async def _lock_version(
        self, proc: Process, n: ProcessNode, commit: CommitInfo, content: str
    ) -> ArtifactVersion:
        """把产物路径的这个提交登记为本次评审的版本（调用方已持有流程锁）。"""
        source, user_uuid = await self._source(n, commit)
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
        a = ArtifactVersion(
            node_uuid=n.uuid,
            version_no=no,
            round=n.round,
            path=n.artifact_path or "",
            commit_sha=commit.sha,
            content=content,
            source=source,
            created_by=user_uuid,
            note=commit.message.splitlines()[0][:200] if commit.message else None,
            author_name=commit.author_name[:100] or None,
            author_login=commit.author_login,
            committed_at=commit.committed_at,
            web_url=commit.web_url,
        )
        self.session.add(a)
        await self.session.flush()
        n.current_artifact_uuid = a.uuid
        self.emit(
            proc, "artifact.locked", n.node_id, version=no, source=source, commit=commit.sha[:10]
        )
        return a

    # ───────────────────────────── 评审

    async def _open_round(
        self, proc: Process, n: ProcessNode, stage: str, review: dict[str, Any]
    ) -> ReviewRound:
        users, _ = await self.assignees(proc.project_uuid, review["role"])
        rr = ReviewRound(
            node_uuid=n.uuid,
            stage=stage,
            # 准入发生在本节点开工之前：记为即将开始的那一轮
            round=n.round if stage == "exit" else n.round + 1,
            role=review["role"],
            rule=review["rule"],
            reviewers=[str(u) for u in users],
            # 准入审核的是上游各节点当前锁定的版本，不绑定本节点的版本
            artifact_uuid=n.current_artifact_uuid if stage == "exit" else None,
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
        """提交准出评审：锁定产物路径当前最新的 commit 作为本次评审的版本。
        评审人名单为空的待评审轮次可以重新提交以刷新名单。

        ★ 只在这里访问托管平台（run 结束与运行中都不看仓库）；先在锁外读，再加锁校验。
        """
        pre = await self.get(process_uuid)
        pre_node = await self.node(pre, node_id)
        latest = (
            await self._latest_commit(pre, pre_node)
            if pre_node.status == "working" and pre_node.artifact_path
            else None
        )
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
        if latest is None:
            raise Conflict("NO_ARTIFACT", "节点还没有产物路径")
        commit, content = latest
        if await self.session.scalar(
            select(ArtifactVersion.id).where(
                ArtifactVersion.node_uuid == n.uuid, ArtifactVersion.commit_sha == commit.sha
            )
        ):
            # 驳回 / 打回后回到协同中：产物路径必须有新的提交
            raise Conflict("ARTIFACT_NOT_REVISED", "上次评审之后产物还没有新的提交")
        await self._lock_version(proc, n, commit, content)
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
            if rr.stage == "admit" and not return_to:
                # 准入驳回 = 打回上游（本节点还没开工）；只有一个上游时默认打回它
                ups = deps.get(n.node_id, [])
                if len(ups) != 1:
                    raise Invalid("RETURN_TARGET_REQUIRED", "准入驳回需要选择打回到哪个上游节点")
                return_to = ups[0]
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
                text = f"{'准出' if rr.stage == 'exit' else '准入'}评审驳回，" + (
                    f"请按意见修改产物：\n\n{comment}\n\n"
                    + git_steps(
                        await self._repo_info(proc),
                        n,
                        commit_template(proc, n, await self._agent_name(n)),
                    )
                )
                self._queue(
                    n, "system", await self._with_pending_kickoff(proc, n, text, "本轮指令")
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
            if rr.stage == "admit":
                # 准入通过：本节点开工；上游的下游都开工后上游通过
                nodes = await self.nodes(proc)
                await self._start_node(proc, n, definition, deps, nodes)
                self._settle_upstreams(proc, deps, nodes, n.node_id)
            elif any(n.node_id in ups for ups in deps.values()):
                n.status, n.notice = "exited", None  # 待下游准入
                self.emit(proc, "node.exited", n.node_id)
            else:
                n.status, n.passed_at, n.notice = "passed", utcnow(), None  # 最后一个节点
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
        """打回：目标节点回到人机协同中；它下游已开工 / 已通过 / 准入中的节点 → 已打回。"""
        via = "准入" if n.status == "admit_review" else ""  # 下面会把 n 也标为已打回，先记下
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
        text = (
            f"下游节点「{n.name}」{via}评审时打回到本节点，"
            f"请按意见修改产物：\n\n{comment}\n\n"
            + git_steps(
                await self._repo_info(proc),
                target,
                commit_template(proc, target, await self._agent_name(target)),
            )
        )
        self._queue(
            target, "system", await self._with_pending_kickoff(proc, target, text, "本轮指令")
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
                visible[tid] = LEVEL_RANK[await self.teams.level(tid)] >= LEVEL_RANK["WRITE"]
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
