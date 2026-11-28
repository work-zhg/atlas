"""节点对话接口（/api/v1）：节点页对话框里发的消息、接管与交还。只收这三件事。

    POST …/nodes/{节点}/messages    对话组件的 submitRun：核对执行人后当场开 run，记 run 号
    POST …/nodes/{节点}/takeover    接管：数字员工停下，人直接改流程空间里的文件
    POST …/nodes/{节点}/handback    交还：说明作为下一条指令

★ 看对话、回答提问和审批、接管浏览器、停止都由对话组件直连 Agent 平台，不经这里。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from ..access import Action
from ..errors import Forbidden
from .deps import AccessDep, PrincipalDep, ServicesDep

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["node-chat"])


class MessageIn(BaseModel):
    #: Agent 平台消息的 content 块：[{type: text, text}, …]
    content: list[dict[str, Any]] = Field(min_length=1)
    idempotency_key: str | None = None


class HandbackIn(BaseModel):
    note: str = ""


async def _executor(svc: Any, access: Any, p: Any, process_id: UUID, node_id: str) -> None:
    proc = await svc.process.get(process_id)
    await access.require(p, Action.PROCESS_RUN, proc.team_uuid)
    n = await svc.process.node(proc, node_id)
    if p.uid not in await svc.process.executors(proc, n):
        raise Forbidden("NOT_EXECUTOR", "只有这个节点的执行人可以做这件事")


@router.post("/processes/{process_id}/nodes/{node_id}/messages", status_code=202)
async def send_message(
    process_id: UUID,
    node_id: str,
    body: MessageIn,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str]:
    await _executor(svc, access, p, process_id, node_id)
    key = body.idempotency_key or idempotency_key or str(uuid4())
    run_id = await svc.collab.send_human(process_id, node_id, body.content, author=p.uid, idem=key)
    return {"run_id": run_id}


@router.post("/processes/{process_id}/nodes/{node_id}/takeover")
async def take_over(
    process_id: UUID, node_id: str, p: PrincipalDep, access: AccessDep, svc: ServicesDep
) -> dict[str, bool]:
    await _executor(svc, access, p, process_id, node_id)
    await svc.collab.take_over(process_id, node_id, actor=p.uid)
    return {"ok": True}


@router.post("/processes/{process_id}/nodes/{node_id}/handback")
async def hand_back(
    process_id: UUID,
    node_id: str,
    body: HandbackIn,
    p: PrincipalDep,
    access: AccessDep,
    svc: ServicesDep,
) -> dict[str, bool]:
    await _executor(svc, access, p, process_id, node_id)
    await svc.collab.hand_back(process_id, node_id, body.note, actor=p.uid)
    return {"ok": True}
