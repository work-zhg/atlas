"""流程运行：项目下的流程列表与发起、流程详情、节点协同、产物、评审、终止、实时事件、待我处理。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import (
    FileTemplate,
    FileTemplateVersion,
    Process,
    ProcessEvent,
    Project,
    Team,
    TeamAgent,
    TfUser,
)
from ..db.session import get_sessionmaker
from ..errors import NotFound
from ..identity import LEVEL_RANK
from ..process.bus import coordinator
from ..process.service import ProcessService
from ..templates import structure
from .deps import TeamsDep
from .views import iso

router = APIRouter(prefix="/api/v1", tags=["processes"])


def _svc(request: Request, teams: TeamsDep) -> ProcessService:
    return ProcessService(teams, request.app.state.git)


ProcessesDep = Annotated[ProcessService, Depends(_svc)]


class StartBody(BaseModel):
    title: str
    requirement: str


class MessageBody(BaseModel):
    text: str


class ArtifactBody(BaseModel):
    content: str
    note: str | None = None


class VoteBody(BaseModel):
    decision: str
    comment: str | None = None
    return_to: str | None = None


class TerminateBody(BaseModel):
    reason: str | None = None


STAGE = {
    "pending": "待开始",
    "working": "人机协同中",
    "exit_review": "待准出",
    "admit_review": "待准入",
    "passed": "已通过",
    "returned": "已打回",
}


async def _names(svc: ProcessService, ids: set[Any]) -> dict[str, str]:
    ids = {UUID(str(i)) for i in ids if i}
    if not ids:
        return {}
    rows = await svc.session.scalars(select(TfUser).where(TfUser.uuid.in_(ids)))
    return {str(u.uuid): u.name for u in rows}


async def _summary(svc: ProcessService, proc: Process) -> dict[str, Any]:
    """列表卡片：卡在哪、等谁（团队设计 §10）。"""
    nodes = await svc.nodes(proc)
    active = [n for n in nodes.values() if n.status in ("working", "exit_review", "admit_review")]
    waiting = []
    for n in active:
        if n.status == "working":
            waiting.append({"node": n.name, "stage": STAGE[n.status], "who": n.exec_role})
        else:
            rr = await svc.open_round(n)
            waiting.append({"node": n.name, "stage": STAGE[n.status], "who": rr.role if rr else ""})
    starter = await svc.session.scalar(select(TfUser.name).where(TfUser.uuid == proc.started_by))
    return {
        "id": str(proc.uuid),
        "no": proc.no,
        "title": proc.title,
        "status": proc.status,
        "started_by": starter,
        "started_at": iso(proc.started_at),
        "finished_at": iso(proc.finished_at),
        "progress": {
            "passed": sum(n.status == "passed" for n in nodes.values()),
            "total": len(nodes),
        },
        "waiting": waiting,
        "notices": [f"{n.name}：{n.notice}" for n in nodes.values() if n.notice],
    }


@router.get("/projects/{pid}/processes")
async def list_processes(
    pid: UUID,
    svc: ProcessesDep,
    filter: str = "all",
    q: str | None = None,
) -> list[dict[str, Any]]:
    status = {"running": "running", "done": "completed"}.get(filter)
    rows = await svc.list(pid, status=status, mine=filter == "todo", q=q)
    return [await _summary(svc, p) for p in rows]


@router.post("/projects/{pid}/processes", status_code=201)
async def start_process(pid: UUID, body: StartBody, svc: ProcessesDep) -> dict[str, Any]:
    proc = await svc.start(pid, body.title, body.requirement)
    return await _summary(svc, proc)


@router.get("/todo")
async def todo(svc: ProcessesDep) -> list[dict[str, Any]]:
    return await svc.todos()


@router.get("/processes/{proc_id}")
async def detail(proc_id: UUID, svc: ProcessesDep) -> dict[str, Any]:
    proc = await svc.get(proc_id)
    definition = await svc.definition(proc)
    nodes = await svc.nodes(proc)
    me = str(svc.p.uuid)
    out_nodes = {}
    for nid, n in nodes.items():
        users, _ = await svc.assignees(proc.project_uuid, n.exec_role)
        rr = await svc.open_round(n)
        agent = None
        if n.agent_uuid:
            agent = await svc.session.scalar(
                select(TeamAgent.name).where(TeamAgent.uuid == n.agent_uuid)
            )
        cur = None
        if n.current_artifact_uuid:
            a = await svc._artifact(n.current_artifact_uuid)
            cur = {"version": a.version_no, "round": a.round, "commit": a.commit_sha[:10]}
        out_nodes[nid] = {
            "status": n.status,
            "stage": STAGE[n.status],
            "round": n.round,
            "notice": n.notice,
            "agent": agent,
            "agent_running": n.atlas_run_id is not None,
            "artifact": cur,
            "executor": me in {str(u) for u in users},
            "reviewer": bool(rr and me in rr.reviewers),
        }
    level = await svc.teams.level(proc.team_uuid)
    starter = await svc.session.scalar(select(TfUser.name).where(TfUser.uuid == proc.started_by))
    return {
        **(await _summary(svc, proc)),
        "requirement": proc.requirement,
        "team_id": str(proc.team_uuid),
        "team_name": await svc.session.scalar(select(Team.name).where(Team.uuid == proc.team_uuid)),
        "project_id": str(proc.project_uuid),
        "project_name": await svc.session.scalar(
            select(Project.name).where(Project.uuid == proc.project_uuid)
        ),
        "template_version": await svc.version_label(proc),
        "definition": definition,
        "deps": structure.derive_deps(definition["flow"]),
        "nodes": out_nodes,
        "event_seq": proc.event_seq,
        "started_by_name": starter,
        "can": {
            "terminate": proc.status == "running"
            and svc.p.has("process:terminate")
            and (proc.started_by == svc.p.uuid or LEVEL_RANK[level] >= LEVEL_RANK["OWNER"]),
        },
    }


@router.get("/processes/{proc_id}/nodes/{node_id}")
async def node_detail(proc_id: UUID, node_id: str, svc: ProcessesDep) -> dict[str, Any]:
    proc = await svc.get(proc_id)
    n = await svc.node(proc, node_id)
    definition = await svc.definition(proc)
    nd = definition["nodes"][node_id]
    deps = structure.derive_deps(definition["flow"])
    nodes = await svc.nodes(proc)
    msgs = await svc.messages(n)
    arts = await svc.artifacts(n)
    reviews = await svc.reviews(n)
    names = await _names(
        svc,
        {m.author_uuid for m in msgs}
        | {a.created_by for a in arts}
        | {v.user_uuid for _, vs in reviews for v in vs}
        | {u for r, _ in reviews for u in r.reviewers},
    )
    ft = None
    if n.file_template_version_uuid:
        row = (
            await svc.session.execute(
                select(FileTemplate.uuid, FileTemplate.name, FileTemplateVersion.version_no)
                .join(FileTemplate, FileTemplate.uuid == FileTemplateVersion.template_uuid)
                .where(FileTemplateVersion.uuid == n.file_template_version_uuid)
            )
        ).one_or_none()
        if row:
            ft = {"id": str(row[0]), "name": row[1], "version": row[2]}
    inputs = []
    for up in deps.get(node_id, []):
        un = nodes[up]
        a = await svc._artifact(un.current_artifact_uuid) if un.current_artifact_uuid else None
        inputs.append(
            {
                "node_id": up,
                "node": un.name,
                "output": definition["nodes"][up]["output_name"],
                "version": a.version_no if a else None,
            }
        )
    users, _ = await svc.assignees(proc.project_uuid, n.exec_role)
    me = str(svc.p.uuid)
    open_rr = next((r for r, _ in reviews if r.status == "open"), None)
    voted = bool(
        open_rr
        and any(str(v.user_uuid) == me for r, vs in reviews if r.uuid == open_rr.uuid for v in vs)
    )
    return {
        "node_id": node_id,
        "name": n.name,
        "config": nd,
        "status": n.status,
        "stage": STAGE[n.status],
        "round": n.round,
        "notice": n.notice,
        "agent_running": n.atlas_run_id is not None,
        "file_template": ft,
        "inputs": inputs,
        "upstream": sorted(_ancestors_of(deps, node_id)),
        "messages": [
            {
                "id": str(m.uuid),
                "role": m.role,
                "author": names.get(str(m.author_uuid)) if m.author_uuid else None,
                "content": m.content,
                "status": m.status,
                "round": m.round,
                "created_at": iso(m.created_at),
            }
            for m in msgs
        ],
        "artifacts": [
            {
                "version": a.version_no,
                "round": a.round,
                "commit": a.commit_sha,
                "path": a.path,
                "source": a.source,
                "by": names.get(str(a.created_by)) if a.created_by else None,
                "note": a.note,
                "current": a.uuid == n.current_artifact_uuid,
                "url": svc.git.web_url(proc.team_uuid, proc.project_uuid, a.commit_sha, a.path),
                "created_at": iso(a.created_at),
            }
            for a in arts
        ],
        "current_content": next(
            (a.content for a in arts if a.uuid == n.current_artifact_uuid), None
        ),
        "reviews": [
            {
                "id": str(r.uuid),
                "stage": r.stage,
                "round": r.round,
                "role": r.role,
                "rule": r.rule,
                "status": r.status,
                "artifact_version": next(
                    (a.version_no for a in arts if a.uuid == r.artifact_uuid), None
                ),
                "reviewers": [{"id": u, "name": names.get(u, "未知用户")} for u in r.reviewers],
                "votes": [
                    {
                        "user": names.get(str(v.user_uuid)),
                        "user_id": str(v.user_uuid),
                        "decision": v.decision,
                        "comment": v.comment,
                        "return_to": v.return_to,
                        "created_at": iso(v.created_at),
                    }
                    for v in vs
                ],
                "opened_at": iso(r.opened_at),
                "closed_at": iso(r.closed_at),
            }
            for r, vs in reviews
        ],
        "can": {
            "handle": proc.status == "running"
            and n.status == "working"
            and me in {str(u) for u in users}
            and svc.p.has("process:handle"),
            "vote": proc.status == "running"
            and open_rr is not None
            and me in open_rr.reviewers
            and not voted
            and svc.p.has("process:review"),
            "resubmit": proc.status == "running"
            and open_rr is not None
            and not open_rr.reviewers
            and me in {str(u) for u in users},
        },
    }


def _ancestors_of(deps: dict[str, list[str]], nid: str) -> set[str]:
    out: set[str] = set()
    stack = list(deps.get(nid, []))
    while stack:
        u = stack.pop()
        if u not in out:
            out.add(u)
            stack.extend(deps.get(u, []))
    return out


@router.get("/processes/{proc_id}/nodes/{node_id}/artifacts/{version}")
async def artifact(proc_id: UUID, node_id: str, version: int, svc: ProcessesDep) -> dict[str, Any]:
    proc = await svc.get(proc_id)
    n = await svc.node(proc, node_id)
    a = next((a for a in await svc.artifacts(n) if a.version_no == version), None)
    if a is None:
        raise NotFound("ARTIFACT_NOT_FOUND", "产物版本不存在")
    return {"version": a.version_no, "commit": a.commit_sha, "path": a.path, "content": a.content}


@router.post("/processes/{proc_id}/nodes/{node_id}/messages", status_code=201)
async def send_message(
    proc_id: UUID, node_id: str, body: MessageBody, svc: ProcessesDep
) -> dict[str, Any]:
    m = await svc.send_message(proc_id, node_id, body.text)
    return {"id": str(m.uuid), "status": m.status}


@router.post("/processes/{proc_id}/nodes/{node_id}/artifacts", status_code=201)
async def submit_artifact(
    proc_id: UUID, node_id: str, body: ArtifactBody, svc: ProcessesDep
) -> dict[str, Any]:
    a = await svc.submit_artifact(proc_id, node_id, body.content, body.note)
    return {"version": a.version_no, "commit": a.commit_sha}


@router.post("/processes/{proc_id}/nodes/{node_id}/submit-review")
async def submit_review(proc_id: UUID, node_id: str, svc: ProcessesDep) -> dict[str, Any]:
    n = await svc.submit_review(proc_id, node_id)
    return {"status": n.status}


@router.post("/processes/{proc_id}/nodes/{node_id}/votes")
async def vote(proc_id: UUID, node_id: str, body: VoteBody, svc: ProcessesDep) -> dict[str, Any]:
    n = await svc.vote(proc_id, node_id, body.decision, body.comment, body.return_to)
    return {"status": n.status}


@router.post("/processes/{proc_id}/terminate")
async def terminate(proc_id: UUID, body: TerminateBody, svc: ProcessesDep) -> dict[str, Any]:
    p = await svc.terminate(proc_id, body.reason)
    return {"status": p.status}


@router.get("/processes/{proc_id}/stream")
async def stream(
    proc_id: UUID,
    svc: ProcessesDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """流程实时事件（SSE）：先按游标回放事件表，再接进程内广播。

    ★ 鉴权后立刻还掉请求级数据库连接：SSE 可以挂很久，每次取事件用短会话。
    """
    proc = await svc.get(proc_id)
    cursor = after if after is not None else proc.event_seq
    if last_event_id and last_event_id.isdigit():
        cursor = int(last_event_id)
    pid = proc.uuid
    await svc.session.commit()
    await svc.session.close()
    return StreamingResponse(
        _events(pid, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _events(pid: UUID, cursor: int) -> AsyncIterator[str]:
    coord = coordinator()
    q = coord.subscribe(pid)
    try:
        while True:
            async with get_sessionmaker()() as s:
                rows = list(
                    await s.scalars(
                        select(ProcessEvent)
                        .where(ProcessEvent.process_uuid == pid, ProcessEvent.seq > cursor)
                        .order_by(ProcessEvent.seq)
                        .limit(500)
                    )
                )
            for r in rows:
                cursor = r.seq
                payload = {
                    "seq": r.seq,
                    "type": r.type,
                    "node_id": r.node_id,
                    "data": r.data,
                    "at": iso(r.created_at),
                }
                data = json.dumps(payload, ensure_ascii=False)
                yield f"id: {r.seq}\nevent: process\ndata: {data}\n\n"
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)  # 心跳间隔，不是等待上限
            except TimeoutError:
                yield ": ping\n\n"
                continue
            while True:
                if msg.get("kind") == "delta":
                    yield f"event: delta\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if q.empty():
                    break
                msg = q.get_nowait()
    finally:
        coord.unsubscribe(pid, q)
