"""系统：登记了哪些系统，以及调各系统运营接口的「系统代理」。

    GET  /api/systems                               各系统的编码、名字、能力
    ANY  /api/systems/{system}/ops/{path}           原样转发到这个系统的 /ops/v1/{path}，
                                                     用它的运营密钥签名、带上运营人员

★ 只转发白名单里的接口（gateway.proxy_allowed）：应用目录、容量、系统参数、系统任务、运行环境、
  技能市场、记忆作用域。挂起、恢复、删除、建租户只能经租户操作，不能从这里绕过去。
★ 写请求要运营管理员，并记一条审计（参数摘要；响应里的密钥、临时密码不进审计）。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, Response

from ..audit import record
from ..errors import Forbidden, Invalid, NotFound, Upstream
from ..gateway import CallFailed, has_dot_segments, proxy_allowed, stamp_of
from .deps import ClientDep, OperatorDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api/systems", tags=["systems"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@router.get("")
async def list_systems(
    _: OperatorDep, client: ClientDep, settings: SettingsDep
) -> list[dict[str, Any]]:
    return [
        {**e.view(), "key_configured": bool(settings.system_keys.get(e.code))}
        for e in client.registry.all()
    ]


@router.api_route("/{system}/ops/{path:path}", methods=_METHODS)
async def proxy(
    system: str, path: str, request: Request, p: OperatorDep, session: SessionDep, client: ClientDep
) -> Response:
    entry = client.registry.get(system)
    if entry is None:
        raise NotFound("SYSTEM_NOT_FOUND", f"没有登记系统 {system}")
    method = request.method.upper()
    sub = "/" + path.lstrip("/")
    if has_dot_segments(sub):
        raise Invalid("BAD_PATH", "路径里不能有 . 或 .. 段")
    if not proxy_allowed(entry, method, sub):
        raise Forbidden("NOT_ALLOWED", f"{entry.name} 的 {method} {sub} 不能从这里调")
    write = method != "GET"
    if write and not p.is_admin:
        raise Forbidden("ADMIN_REQUIRED", "运营观察员只能查看；这个操作要运营管理员")
    body = await request.body()
    content_type = request.headers.get("content-type")
    stamp = stamp_of(p.operator_uuid, p.name, idempotency_key=uuid4() if write else None)
    try:
        reply = await client.request(
            system,
            method,
            sub,
            body=body,
            content_type=content_type,
            params=dict(request.query_params),
            stamp=stamp,
        )
    except CallFailed as exc:
        raise Upstream("UPSTREAM_UNAVAILABLE", exc.message) from exc
    if write:
        record(
            session,
            p.who,
            f"system.{method.lower()}",
            target=f"{system} {sub}"[:200],
            detail=_summary(body, content_type),
            result="OK" if reply.status < 400 else "FAILED",
        )
    return Response(
        content=reply.body, status_code=reply.status, media_type=reply.content_type or None
    )


def _summary(body: bytes, content_type: str | None) -> dict[str, Any] | None:
    """审计里的参数摘要：JSON 原样（去掉密钥类字段，见 audit._clean），上传的文件只记大小。"""
    if not body:
        return None
    if content_type and content_type.startswith("application/json") and len(body) <= 16_384:
        try:
            data = json.loads(body)
        except ValueError:
            return {"bytes": len(body)}
        return data if isinstance(data, dict) else {"body": data}
    return {"bytes": len(body), "content_type": (content_type or "").split(";")[0]}
