"""人机协同：按节点限定的 Atlas 会话代理。

TeamFlow 前端的对话区就是 Atlas 的对话组件（@atlas/chat-ui，与 Atlas 前端同一份实现），
它以 `/api/v1/processes/{流程}/nodes/{节点}/atlas` 为接口根，请求与 Atlas 原样相同的路径：

  GET  v1/threads/{会话}                    会话详情
  GET  v1/threads/{会话}/messages           消息分页
  GET  v1/threads/{会话}/events             会话事件流（SSE，透传）
  POST v1/threads/{会话}/runs               发一条指令            ← 执行人
  GET  v1/runs/{运行}[/trace|/approvals]    运行详情 / 轨迹 / 待审批
  POST v1/runs/{运行}/cancel                取消                  ← 执行人
  POST v1/runs/{运行}/approvals/{审批}      审批决定              ← 执行人

★ 只放行**该节点**的会话，以及属于它（或它的子智能体会话）的运行；其余一律 404。
★ 读：能看见流程即可（团队成员）。写：操作码 process:handle + 在执行角色的当前分配中 + 流程进行中。
★ 经代理发出的指令登记为节点的当前运行（tf_node_message + atlas_run_id），由 Agent 监督器
  跟到结束、落库回复、观察 Agent 向产物路径的提交 —— 与系统指令走同一条路。
★ 错误用 Atlas 的信封 {"error": {kind, message}}：对话组件按 Atlas 的格式解包并原样显示。
★ 身份：转发时用 TeamFlow 的 Atlas 服务身份（X-User-Id），不透传浏览器的任何头 / Cookie。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from ..db.models import NodeMessage, Process, ProcessNode
from ..errors import TFError
from ..process.bus import wake_after_commit
from ..process.service import ProcessService
from .deps import AtlasDep, TeamsDep

router = APIRouter(prefix="/api/v1", tags=["atlas-proxy"])

_THREAD = re.compile(r"^v1/threads/(?P<tid>[0-9a-f-]{36})(?P<rest>/messages|/events|/runs)?$")
_RUN = re.compile(
    r"^v1/runs/(?P<rid>[0-9a-f-]{36})(?P<rest>/trace|/approvals|/cancel|/approvals/[0-9a-f-]{36})?$"
)
_WRITE = {("POST", "/runs"), ("POST", "/cancel"), ("POST", "/approvals/*")}

#: 运行 → 是否属于该节点会话（运行的归属不会变，进程内缓存即可）
_run_owner: dict[tuple[str, str], bool] = {}


def _err(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"kind": kind, "message": message}}, status_code=status)


def _passthrough(r: Any) -> Response:
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


async def _owns_run(atlas: AtlasDep, thread_id: str, run_id: str) -> bool:
    key = (thread_id, run_id)
    if key not in _run_owner:
        r = await atlas.raw("GET", f"/v1/runs/{run_id}")
        if r.status_code != 200:
            return False
        run_thread = str(r.json().get("thread_id"))
        owned = run_thread == thread_id
        if not owned:  # 子智能体的运行在子会话上：看子会话的父会话
            t = await atlas.raw("GET", f"/v1/threads/{run_thread}")
            owned = t.status_code == 200 and str(t.json().get("parent_thread_id")) == thread_id
        _run_owner[key] = owned
    return _run_owner[key]


@router.api_route("/processes/{proc_id}/nodes/{node_id}/atlas/{path:path}", methods=["GET", "POST"])
async def proxy(
    proc_id: UUID, node_id: str, path: str, request: Request, teams: TeamsDep, atlas: AtlasDep
) -> Response:
    svc = ProcessService(teams, request.app.state.repo)
    try:
        return await _proxy(svc, atlas, proc_id, node_id, path, request)
    except TFError as exc:
        return _err(exc.status_code, exc.code.lower(), exc.message)


async def _proxy(
    svc: ProcessService, atlas: AtlasDep, proc_id: UUID, node_id: str, path: str, request: Request
) -> Response:
    method = request.method
    proc = await svc.get(proc_id)
    node = await svc.node(proc, node_id)
    thread = node.atlas_thread_id
    if not thread:
        return _err(404, "no_thread", "这个节点的 Agent 会话还没有建立")

    if m := _THREAD.match(path):
        if m["tid"] != thread:
            return _err(404, "not_found", "会话不属于这个节点")
        rest = m["rest"] or ""
    elif m := _RUN.match(path):
        rest = m["rest"] or ""
        if not await _owns_run(atlas, thread, m["rid"]):
            return _err(404, "not_found", "运行不属于这个节点的会话")
    else:
        return _err(404, "not_found", "不支持的接口")

    kind = "/approvals/*" if rest.startswith("/approvals/") else rest
    write = (method, kind) in _WRITE
    if method == "POST" and not write:
        return _err(405, "method_not_allowed", "只读接口")
    if write:
        svc._require_running(proc)
        await svc._require_executor(proc, node)

    params = dict(request.query_params)
    fwd = {
        k: v
        for k, v in request.headers.items()
        if k.lower() in ("idempotency-key", "last-event-id", "accept")
    }
    # 代表当前用户（Atlas 只用于审计「这一轮是谁触发的」，不扩大访问）
    fwd["X-Atlas-Acting-User"] = str(svc.p.uuid)

    # ── 会话事件流：鉴权完立刻归还数据库连接，再透传（SSE 可以挂很久）
    if rest == "/events":
        await svc.session.commit()
        await svc.session.close()
        status, body = await atlas.raw_stream(f"/{path}", params=params, headers=fwd)
        if status != 200:
            chunks = [c async for c in body]
            return Response(b"".join(chunks), status_code=status, media_type="application/json")
        return StreamingResponse(
            _relay(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    json_body = await request.json() if method == "POST" and await request.body() else None

    # ── 人发一条指令：登记为节点的当前运行，交给监督器跟随
    if rest == "/runs":
        return await _send(svc, atlas, proc, node, path, json_body, fwd)

    r = await atlas.raw(method, f"/{path}", params=params, json_body=json_body, headers=fwd)
    return _passthrough(r)


async def _relay(body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    async for chunk in body:
        yield chunk


async def _send(
    svc: ProcessService,
    atlas: AtlasDep,
    proc: Process,
    node: ProcessNode,
    path: str,
    json_body: Any,
    fwd: dict[str, str],
) -> Response:
    # 流程行锁：与状态迁移、监督器落库串行
    proc = await svc.get(proc.uuid, lock=True)
    node = await svc.node(proc, node.node_id)
    if node.status != "working":
        return _err(409, "node_not_working", "节点不在人机协同中，不能下指令")
    if node.atlas_run_id:
        return _err(409, "run_in_progress", "Agent 正在运行，请等它结束或先取消")
    r = await atlas.raw("POST", f"/{path}", json_body=json_body, headers=fwd)
    if r.status_code not in (200, 202):
        return _passthrough(r)
    run_id = str(r.json()["run_id"])
    text = "\n".join(
        str(b.get("text") or "")
        for b in (json_body or {}).get("content", [])
        if b.get("type") == "text"
    )
    exists = await svc.session.scalar(
        select(NodeMessage.id).where(
            NodeMessage.node_uuid == node.uuid, NodeMessage.atlas_run_id == run_id
        )
    )
    if exists is None:  # 幂等键重发拿到的是同一个 run：不重复登记
        svc.session.add(
            NodeMessage(
                node_uuid=node.uuid,
                role="user",
                author_uuid=svc.actor,
                content=text,
                status="done",
                atlas_run_id=run_id,
                round=node.round,
            )
        )
        svc.session.add(
            NodeMessage(
                node_uuid=node.uuid,
                role="agent",
                content="",
                atlas_run_id=run_id,
                status="streaming",
                round=node.round,
            )
        )
        node.atlas_run_id = run_id
        if node.notice and node.notice.startswith("Agent"):
            node.notice = None
        svc.emit(proc, "message.sent", node.node_id, run=run_id)
        wake_after_commit(svc.session.sync_session, node.uuid)
    await svc.session.flush()
    return _passthrough(r)
