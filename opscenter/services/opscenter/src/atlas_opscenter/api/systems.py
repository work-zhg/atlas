"""系统：登记了哪些系统，以及调各系统运营接口的「系统代理」。

    GET  /api/systems                               各系统的编码、名字、能力、模块
    POST /api/systems/{system}/backfill             新登记的系统：给现有每个租户补建一次
                                                     （租户模块读写）
    ANY  /api/systems/{system}/proxy/{path}         原样转发到这个系统的 /ops/v1/{path}，
                                                     用它的运营密钥签名、带上运营人员

★ 路径里不能有 /ops/ 段：API 网关对含 /ops/ 段的路径一律 404（各系统的运营接口不对外）。
★ 只转发白名单里的接口（gateway.proxy_allowed）：应用目录、容量、系统参数、系统任务、运行环境、
  技能市场、记忆作用域。建租户、创建、删除、挡住入口只能经租户操作，不能从这里绕过去。
★ 按模块判级别（access.py）：模块是这个系统的（用户中心的 /tenants/… 属于租户模块）；GET 要只读，
  其余要读写，轮换凭据、删目录里的东西要 Owner。
★ 写请求记一条审计（参数摘要；密钥、临时密码不进审计）。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, Response

from ..access import required_level
from ..audit import record
from ..errors import Forbidden, Invalid, NotFound, Upstream
from ..gateway import CallFailed, proxy_allowed, sent_as_is, stamp_of
from ..tenants import TenantOps
from .deps import CatalogDep, ClientDep, PrincipalDep, SessionDep, SettingsDep, TenantWriteDep

router = APIRouter(prefix="/api/systems", tags=["systems"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@router.get("")
async def list_systems(
    _: PrincipalDep, client: ClientDep, settings: SettingsDep
) -> list[dict[str, Any]]:
    return [
        {**e.view(), "key_configured": bool(settings.system_keys.get(e.code))}
        for e in client.registry.all()
    ]


@router.post("/{system}/backfill", status_code=202)
async def backfill(
    system: str, p: TenantWriteDep, session: SessionDep, client: ClientDep
) -> dict[str, Any]:
    """新登记的系统：对现有每个租户发起一个「这个系统创建」的操作（创建能重做，补多了也没事）。
    → {started, skipped}：skipped 是还有进行中操作、这次没补的企业代码，之后再补一次。"""
    return await TenantOps(session, client).backfill(system, p.who)


@router.api_route("/{system}/proxy/{path:path}", methods=_METHODS)
async def proxy(
    system: str,
    path: str,
    request: Request,
    p: PrincipalDep,
    session: SessionDep,
    client: ClientDep,
    catalog: CatalogDep,
) -> Response:
    entry = client.registry.get(system)
    if entry is None:
        raise NotFound("SYSTEM_NOT_FOUND", f"没有登记系统 {system}")
    method = request.method.upper()
    sub = "/" + path.lstrip("/")
    if not sent_as_is(sub):
        raise Invalid("BAD_PATH", "路径里不能有点段、? 或 #")
    if not proxy_allowed(entry, method, sub):
        raise Forbidden("NOT_ALLOWED", f"{entry.name} 的 {method} {sub} 不能从这里调")
    module = catalog.for_proxy(entry, sub)
    p.require(module, required_level(entry, method, sub))
    write = method != "GET"
    body = await request.body()
    content_type = request.headers.get("content-type")
    stamp = stamp_of(p.user_uuid, p.name, idempotency_key=uuid4() if write else None)
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
            module=module.code,
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
