"""流程接口（/api/v1）：发起、流程列表与详情、首节点开始、提交评审、评审、终止、追溯、实时事件、
流程空间的文件、等你处理与通知；还有「我」和选人。

★ 流程的执行人、评审人是流程运行判的（模板角色，项目里分配）；这里只判操作码 ∧ 团队级别。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal
from uuid import UUID

from atlas_tenancy import TenantScope
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..access import LEVEL_RANK, Action
from ..coord import coordinator
from ..db.session import get_sessionmaker
from ..errors import Invalid
from .deps import AccessDep, PeopleDep, PrincipalDep, ServicesDep, build_services
from .views import (
    event_out,
    instruction_out,
    node_out,
    notification_out,
    process_out,
    review_out,
    session_out,
    submission_out,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["flow"])

UPLOAD_MAX = 50 * 1024 * 1024


# ───────────────────────────── 我、选人


@router.get("/me")
async def me(p: PrincipalDep, access: AccessDep) -> dict[str, Any]:
    return {
        "id": str(p.uid),
        "name": p.name,
        "tenant_id": str(p.tenant),
        "operations": sorted(p.operations),
        "menus": list(p.menus),
        "system_tenant": p.system_tenant,
        "all_teams_level": p.team_all,
        "market": access.market_level(p).value,
    }


@router.get("/people")
async def people(p: PrincipalDep, svc: ServicesDep, q: str | None = None) -> list[dict[str, Any]]:
    return await svc.teams.members_acl.people(q)


@router.get("/people/names")
async def people_names(p: PrincipalDep, people: PeopleDep, ids: str = "") -> dict[str, Any]:
    uids = []
    for raw in ids.split(","):
        try:
            uids.append(UUID(raw.strip()))
        except ValueError:
            continue
    return await people.names(uids[:200])


# ───────────────────────────── 流程


class StartIn(BaseModel):
    template_id: UUID
    title: str


class SubmitIn(BaseModel):
    paths: list[str] = Field(min_length=1)


class VoteIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None
    return_to: str | None = None


class TerminateIn(BaseModel):
    reason: str | None = None


@router.get("/projects/{project_id}/processes")
async def list_processes(
    project_id: UUID,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
    status: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.PROCESS_VIEW, pr.team_uuid)
    todo = {t.process_uuid for t in await svc.inbox.todos(p.uid, project_uuid=pr.uuid)}
    out = []
    for proc in await svc.process.list(pr.uuid, status=status, q=q):
        nodes = sorted((await svc.process.nodes(proc)).values(), key=lambda n: n.id)
        out.append(
            {
                **process_out(proc),
                "nodes": [
                    {"id": n.node_id, "name": n.name, "status": n.status, "round": n.round}
                    for n in nodes
                ],
                "waiting_for_me": proc.uuid in todo,
            }
        )
    return out


@router.post("/projects/{project_id}/processes", status_code=201)
async def start_process(
    project_id: UUID, body: StartIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    pr = await svc.projects.get(project_id)
    await access.require(p, Action.PROCESS_RUN, pr.team_uuid)
    proc = await svc.process.start(pr.uuid, body.template_id, body.title, actor=p.uid)
    return process_out(proc)


async def _process(svc: Any, access: Any, p: Any, process_id: UUID, action: Action) -> Any:
    proc = await svc.process.get(process_id)
    await access.require(p, action, proc.team_uuid)
    return proc


@router.get("/processes/{process_id}")
async def get_process(
    process_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    proc = await _process(svc, access, p, process_id, Action.PROCESS_VIEW)
    s = await svc.process.structure_of(proc)
    table = await svc.process.roles(proc)
    can_run = (
        p.has(Action.PROCESS_RUN.operation)
        and LEVEL_RANK[await access.team_level(p, proc.team_uuid)] >= LEVEL_RANK["WRITE"]
    )
    nodes = []
    for n in sorted((await svc.process.nodes(proc)).values(), key=lambda x: x.id):
        ns = await svc.collab.session_of(n.uuid)
        executor = p.uid in table.members.get(n.exec_role, [])
        rr = await svc.process.open_review(n)
        last = await svc.process.latest_submission(n)
        nodes.append(
            {
                **node_out(n),
                "executors": [str(u) for u in table.members.get(n.exec_role, [])],
                "i_am_executor": executor and can_run,
                "session": session_out(
                    ns, can_send=executor and can_run and await svc.collab.can_send(n, ns)
                ),
                "open_review": (
                    {
                        "stage": rr.stage,
                        "reviewers": list(rr.reviewers),
                        "i_can_vote": str(p.uid) in rr.reviewers and can_run,
                    }
                    if rr
                    else None
                ),
                "latest_submission": submission_out(last) if last else None,
            }
        )
    return {
        **process_out(proc),
        "structure": s,
        "nodes": nodes,
        "roles": {k: [str(u) for u in v] for k, v in table.members.items()},
        "can_run": can_run,
        "can_terminate": can_run
        and proc.status == "running"
        and (
            proc.started_by == p.uid
            or LEVEL_RANK[await access.team_level(p, proc.team_uuid)] >= LEVEL_RANK["OWNER"]
        ),
    }


@router.get("/processes/{process_id}/nodes/{node_id}")
async def get_node(
    process_id: UUID, node_id: str, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    proc = await _process(svc, access, p, process_id, Action.PROCESS_VIEW)
    n = await svc.process.node(proc, node_id)
    executors = await svc.process.executors(proc, n)
    can_run = (
        p.has(Action.PROCESS_RUN.operation)
        and LEVEL_RANK[await access.team_level(p, proc.team_uuid)] >= LEVEL_RANK["WRITE"]
    )
    ns = await svc.collab.session_of(n.uuid)
    executor = p.uid in executors and can_run
    return {
        **node_out(n),
        "process": process_out(proc),
        "executors": [str(u) for u in executors],
        "i_am_executor": executor,
        "session": session_out(ns, can_send=executor and await svc.collab.can_send(n, ns)),
        "submissions": [submission_out(x) for x in await svc.process.submissions(n)],
        "reviews": [review_out(r, v) for r, v in await svc.process.reviews(n)],
        "instructions": [instruction_out(i) for i in await svc.collab.instructions(n.uuid)],
    }


@router.post("/processes/{process_id}/nodes/{node_id}/begin")
async def begin_node(
    process_id: UUID, node_id: str, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    await _process(svc, access, p, process_id, Action.PROCESS_RUN)
    return node_out(await svc.process.begin(process_id, node_id, actor=p.uid))


@router.post("/processes/{process_id}/nodes/{node_id}/submissions", status_code=201)
async def submit(
    process_id: UUID,
    node_id: str,
    body: SubmitIn,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    await _process(svc, access, p, process_id, Action.PROCESS_RUN)
    rr = await svc.process.submit(process_id, node_id, body.paths, actor=p.uid)
    return review_out(rr, [])


@router.post("/processes/{process_id}/nodes/{node_id}/votes")
async def vote(
    process_id: UUID,
    node_id: str,
    body: VoteIn,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    await _process(svc, access, p, process_id, Action.PROCESS_RUN)
    n = await svc.process.vote(
        process_id, node_id, body.decision, body.comment, body.return_to, actor=p.uid
    )
    return node_out(n)


@router.post("/processes/{process_id}/terminate")
async def terminate(
    process_id: UUID, body: TerminateIn, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, Any]:
    proc = await _process(svc, access, p, process_id, Action.PROCESS_RUN)
    owner = LEVEL_RANK[await access.team_level(p, proc.team_uuid)] >= LEVEL_RANK["OWNER"]
    return process_out(
        await svc.process.terminate(process_id, body.reason, actor=p.uid, is_owner=owner)
    )


@router.get("/processes/{process_id}/events")
async def trace(
    process_id: UUID,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
    after: int = 0,
    node: str | None = None,
    type: list[str] | None = Query(default=None),
) -> list[dict[str, Any]]:
    proc = await _process(svc, access, p, process_id, Action.PROCESS_VIEW)
    return [
        event_out(e) for e in await svc.process.events(proc, after=after, node_id=node, types=type)
    ]


@router.get("/processes/{process_id}/stream")
async def stream(
    process_id: UUID,
    request: Request,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
    after: int = 0,
) -> StreamingResponse:
    """本流程的实时事件（SSE）：有新事件就按游标从库里取；15 秒一次心跳。鉴权完先还数据库连接。"""
    proc = await _process(svc, access, p, process_id, Action.PROCESS_VIEW)
    last = request.headers.get("last-event-id")
    cursor = int(last) if last and last.isdigit() else after
    tenant = p.tenant
    await svc.session.commit()
    await svc.session.close()
    uc, atlas = request.app.state.uc, request.app.state.atlas

    async def frames() -> AsyncIterator[bytes]:
        nonlocal cursor
        c = coordinator()
        q = c.subscribe(proc.uuid)
        try:
            yield b": connected\n\n"
            while True:
                with TenantScope.enter(tenant):
                    async with get_sessionmaker()() as s:
                        events = await build_services(s, uc, atlas).process.events(
                            proc, after=cursor
                        )
                for e in events:
                    cursor = e.seq
                    data = json.dumps(event_out(e), ensure_ascii=False)
                    yield f"id: {e.seq}\nevent: {e.type}\ndata: {data}\n\n".encode()
                try:
                    await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield b": ping\n\n"
                if await request.is_disconnected():
                    return
        finally:
            c.unsubscribe(proc.uuid, q)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# ───────────────────────────── 流程空间


@router.get("/processes/{process_id}/space")
async def space_list(
    process_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep, dir: str = ""
) -> dict[str, Any]:
    proc = await _process(svc, access, p, process_id, Action.PROCESS_VIEW)
    got = await svc.spaces.list(proc.space_uuid, dir)
    uploads = {
        e.data.get("path"): str(e.actor)
        for e in await svc.process.events(proc, types=["space.uploaded"], limit=5000)
    }
    return {
        "dir": got["dir"],
        "truncated": got["truncated"],
        "entries": [
            {
                "path": x.path,
                "name": x.name,
                "is_dir": x.is_dir,
                "size": x.size,
                "modified_at": x.modified_at,
                "node": x.node,
                "uploaded_by": uploads.get(x.path),
            }
            for x in got["entries"]
        ],
    }


@router.get("/processes/{process_id}/space/download")
async def space_download(
    process_id: UUID, p: PrincipalDep, access: AccessDep, svc: ServicesDep, path: str
) -> StreamingResponse:
    from urllib.parse import quote

    proc = await _process(svc, access, p, process_id, Action.PROCESS_VIEW)
    size, body = await svc.spaces.read(proc.space_uuid, path)
    name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
    if size:
        headers["Content-Length"] = str(size)
    return StreamingResponse(body, media_type="application/octet-stream", headers=headers)


@router.put("/processes/{process_id}/space", status_code=201)
async def space_upload(
    process_id: UUID,
    request: Request,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
    path: str,
) -> dict[str, Any]:
    proc = await _process(svc, access, p, process_id, Action.PROCESS_RUN)
    data = await request.body()
    if len(data) > UPLOAD_MAX:
        raise Invalid("FILE_TOO_LARGE", f"单个文件不能超过 {UPLOAD_MAX // 1024 // 1024} MB")
    got = await svc.spaces.upload(proc.space_uuid, path, data)
    await svc.process.record(
        proc.uuid, "space.uploaded", actor=p.uid, path=got.get("path") or path, size=len(data)
    )
    return got


# ───────────────────────────── 等你处理与通知


class ReadIn(BaseModel):
    ids: list[UUID] | None = None


class BindIn(BaseModel):
    account: str


@router.get("/todos")
async def todos(
    p: PrincipalDep, svc: ServicesDep, project_id: UUID | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "kind": t.kind,
            "process_id": str(t.process_uuid),
            "process_code": f"F-{t.process_no}",
            "process_title": t.process_title,
            "project_id": str(t.project_uuid),
            "team_id": str(t.team_uuid),
            "node_id": t.node_id,
            "node_name": t.node_name,
            "since": t.since.isoformat(),
        }
        for t in await svc.inbox.todos(p.uid, project_uuid=project_id)
    ]


@router.get("/notifications")
async def notifications(p: PrincipalDep, svc: ServicesDep, unread: bool = False) -> dict[str, Any]:
    return {
        "unread": await svc.inbox.unread(p.uid),
        "items": [
            notification_out(n) for n in await svc.inbox.notifications(p.uid, unread_only=unread)
        ],
    }


@router.post("/notifications/read")
async def mark_read(body: ReadIn, p: PrincipalDep, svc: ServicesDep) -> dict[str, bool]:
    await svc.inbox.mark_read(p.uid, body.ids)
    return {"ok": True}


@router.get("/me/channels")
async def my_channels(p: PrincipalDep, svc: ServicesDep) -> dict[str, str]:
    return {b.channel: b.account for b in await svc.inbox.bindings(p.uid)}


@router.put("/me/channels/{channel}")
async def bind_channel(
    channel: str, body: BindIn, p: PrincipalDep, svc: ServicesDep
) -> dict[str, str]:
    b = await svc.inbox.bind(p.uid, channel, body.account)
    return {b.channel: b.account}


@router.delete("/me/channels/{channel}")
async def unbind_channel(channel: str, p: PrincipalDep, svc: ServicesDep) -> dict[str, bool]:
    await svc.inbox.unbind(p.uid, channel)
    return {"ok": True}
