"""我的设备（c4-model/agent/device/code-device-registry.html · DeviceRoutes）。

    POST   /v1/devices/pairing           要一个配对码（10 分钟内有效、只能用一次）
    GET    /v1/devices                   我的设备（默认设备在前），带在线状态和连接器报告
    PATCH  /v1/devices/{id}              改名
    POST   /v1/devices/{id}/default      设为默认
    DELETE /v1/devices/{id}              吊销：令牌立即失效，设备网关断开它
    GET    /v1/devices/choices           新建本机 CLI 会话时可选的电脑（?cli_type=）
    GET    /v1/devices/{id}/projects     这台电脑每个允许目录下一层的子目录名（选已有项目）

★ 只认本人：别人的设备与不存在一样 404。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from ...auth.authz import PrincipalDep
from ...db.models import Device
from ...db.session import get_sessionmaker
from ...deps import RedisDep, SessionDep, SettingsDep
from ...device.gateway import DeviceGateway, DeviceStatus
from ...device.registry import DeviceService
from ...device.sessions import DeviceSessions
from ...schemas.device import (
    DeviceChoiceOut,
    DeviceListOut,
    DeviceOut,
    DeviceRenameIn,
    PairingCodeOut,
)

router = APIRouter(prefix="/devices", tags=["devices"])


def _sessions(settings: Any) -> DeviceSessions:
    return DeviceSessions(get_sessionmaker(), DeviceGateway(settings))


def _out(device: Device, status_: DeviceStatus | None) -> DeviceOut:
    report = status_.report if status_ is not None else {}
    policy = report.get("policy")
    return DeviceOut(
        id=device.device_uuid,
        name=device.name,
        is_default=device.is_default,
        status="online" if status_ is not None else "offline",
        os=device.os,
        connector_version=device.connector_version,
        paired_at=device.paired_at,
        last_seen_at=device.last_seen_at,
        clis=status_.clis if status_ is not None else {},
        policy=policy if isinstance(policy, dict) else {},
    )


@router.post("/pairing", response_model=PairingCodeOut, status_code=status.HTTP_201_CREATED)
async def start_pairing(
    session: SessionDep, redis: RedisDep, settings: SettingsDep, principal: PrincipalDep
) -> PairingCodeOut:
    service = DeviceService(session, redis=redis, pairing_ttl_s=settings.device_pairing_ttl_s)
    code = await service.start_pairing(principal.uid)
    return PairingCodeOut(code=code.code, expires_at=code.expires_at)


@router.get("", response_model=DeviceListOut)
async def list_devices(
    session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> DeviceListOut:
    devices = await DeviceService(session).list(principal.uid)
    statuses = await _sessions(settings).statuses([str(d.device_uuid) for d in devices])
    return DeviceListOut(data=[_out(d, statuses.get(str(d.device_uuid))) for d in devices])


@router.get("/choices", response_model=list[DeviceChoiceOut])
async def device_choices(
    settings: SettingsDep,
    principal: PrincipalDep,
    cli_type: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[DeviceChoiceOut]:
    choices = await _sessions(settings).choices(principal.uid, cli_type)
    return [
        DeviceChoiceOut(
            id=c.device.device_uuid,
            name=c.device.name,
            is_default=c.device.is_default,
            online=c.online,
            has_cli=c.has_cli,
            selectable=c.selectable,
            allowed_roots=c.status.allowed_roots if c.status is not None else [],
        )
        for c in choices
    ]


@router.get("/{device_id}/projects")
async def device_projects(
    device_id: UUID, settings: SettingsDep, principal: PrincipalDep
) -> dict[str, Any]:
    """{roots: [{root, exists, entries: [{name, path, is_repo}], truncated}]}。"""
    return await _sessions(settings).projects(principal.uid, device_id)


@router.patch("/{device_id}", response_model=DeviceOut)
async def rename_device(
    device_id: UUID,
    payload: DeviceRenameIn,
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> DeviceOut:
    device = await DeviceService(session).rename(principal.uid, device_id, payload.name)
    statuses = await _sessions(settings).statuses([str(device_id)])
    return _out(device, statuses.get(str(device_id)))


@router.post("/{device_id}/default", response_model=DeviceOut)
async def set_default_device(
    device_id: UUID, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> DeviceOut:
    device = await DeviceService(session).set_default(principal.uid, device_id)
    statuses = await _sessions(settings).statuses([str(device_id)])
    return _out(device, statuses.get(str(device_id)))


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: UUID, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> Response:
    event = await DeviceService(session).revoke(principal.uid, device_id)
    await _sessions(settings).on_device_revoked(event)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
