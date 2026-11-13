"""device 表：一个人配对到平台的电脑（c4-model/agent/device/code-device-registry.html）。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Device
from ..db.session import fetch_one

__all__ = ["DeviceRepository"]


class DeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, device_uuid: UUID) -> Device | None:
        return await fetch_one(self._session, Device, Device.device_uuid == device_uuid)

    async def by_token_hash(self, token_hash: str) -> Device | None:
        return await fetch_one(self._session, Device, Device.token_hash == token_hash)

    async def list(self, owner: UUID, *, include_revoked: bool = False) -> list[Device]:
        """默认设备在前，其余按最近连过排（没连过的按配对时间）。"""
        stmt = select(Device).where(Device.owner_uuid == owner)
        if not include_revoked:
            stmt = stmt.where(Device.status == "active")
        stmt = stmt.order_by(
            Device.is_default.desc(),
            Device.last_seen_at.desc().nulls_last(),
            Device.paired_at.desc(),
        )
        return list((await self._session.scalars(stmt)).all())

    async def many(self, ids: set[UUID]) -> dict[UUID, Device]:
        if not ids:
            return {}
        rows = await self._session.scalars(select(Device).where(Device.device_uuid.in_(ids)))
        return {d.device_uuid: d for d in rows}

    async def default_of(self, owner: UUID) -> Device | None:
        return await fetch_one(
            self._session,
            Device,
            Device.owner_uuid == owner,
            Device.is_default.is_(True),
            Device.status == "active",
        )

    def add(self, device: Device) -> None:
        self._session.add(device)

    async def flush(self) -> None:
        await self._session.flush()
