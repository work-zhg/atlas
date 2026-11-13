"""设备登记（c4-model/agent/device/code-device-registry.html）。

一个人的电脑怎么登记到平台：网页上要一个配对码 → 电脑上的连接器拿它经设备网关换设备令牌 →
之后每次连上都凭令牌核对。

    DeviceService        配对码、兑换、核对令牌、我的设备（改名、设默认、吊销）
    PairingCodes         配对码放 Redis：10 分钟内有效、取了就删（只能用一次）
    DeviceToken          设备令牌：原文只在兑换时返回一次，库里只存 sha256
    DefaultDevicePolicy  第一台配对的自动成为默认；吊销的是默认设备时，改成剩下设备里最近连过的一台

★ 只认本人：设备只属于一个人，查、改、吊销都按「主人 = 请求者」过滤，别人的设备与不存在一样 404。
★ 吊销立即生效：令牌失效（核对时按状态拒绝），并由设备会话请设备网关断开已有连接
  （DeviceSessions.on_device_revoked）。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Device
from ..errors import NotFound
from ..repositories.device import DeviceRepository

__all__ = [
    "DefaultDevicePolicy",
    "DeviceIdentity",
    "DeviceRevoked",
    "DeviceService",
    "DeviceToken",
    "IssuedDevice",
    "PairingCode",
    "PairingCodes",
]

_PAIRING_KEY = "atlas:device:pairing:{}"
#: 配对码的字符：去掉容易看错的 0/O、1/I/L，方便在终端里手输
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_HALF = 4
_TOKEN_PREFIX = "dvt_"
_NAME_MAX = 128


@dataclass(frozen=True)
class PairingCode:
    code: str
    owner_uuid: UUID
    expires_at: datetime


@dataclass(frozen=True)
class DeviceIdentity:
    """核对通过的设备：设备网关据此建立控制连接、登记在线。"""

    device_uuid: UUID
    owner_uuid: UUID
    name: str

    def to_json(self) -> dict[str, Any]:
        return {
            "device_uuid": str(self.device_uuid),
            "owner_uuid": str(self.owner_uuid),
            "name": self.name,
        }


@dataclass(frozen=True)
class IssuedDevice:
    """兑换的结果：令牌原文只在这里出现一次。"""

    identity: DeviceIdentity
    token: str

    def to_json(self) -> dict[str, Any]:
        return {**self.identity.to_json(), "token": self.token}


@dataclass(frozen=True)
class DeviceRevoked:
    device_uuid: UUID
    owner_uuid: UUID
    revoked_at: datetime


class DeviceToken:
    @staticmethod
    def issue() -> tuple[str, str]:
        """→ (原文, 摘要)。"""
        raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        return raw, DeviceToken.digest(raw)

    @staticmethod
    def digest(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()


def normalize_code(raw: str) -> str:
    """大小写、空白、有没有中间的短横线都不计较：k7m2q9xd、K7M2-Q9XD 是同一个码。"""
    chars = "".join(ch for ch in raw.upper() if ch.isalnum())
    return f"{chars[:_CODE_HALF]}-{chars[_CODE_HALF:]}" if len(chars) == 2 * _CODE_HALF else chars


class PairingCodes:
    def __init__(self, redis: aioredis.Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl = ttl_s

    async def issue(self, owner: UUID) -> PairingCode:
        while True:
            code = normalize_code(
                "".join(secrets.choice(_CODE_ALPHABET) for _ in range(2 * _CODE_HALF))
            )
            # 撞上别人还没用掉的码（极少）就换一个
            if await self._redis.set(_PAIRING_KEY.format(code), str(owner), ex=self._ttl, nx=True):
                return PairingCode(code, owner, datetime.now(UTC) + timedelta(seconds=self._ttl))

    async def take(self, code: str) -> UUID | None:
        """取了就删：同一个码只能兑换一次。"""
        owner = await self._redis.getdel(_PAIRING_KEY.format(normalize_code(code)))
        return UUID(owner) if owner else None


class DefaultDevicePolicy:
    """每人至多一台默认设备（库里有部分唯一索引兜底）。"""

    def __init__(self, repo: DeviceRepository) -> None:
        self._repo = repo

    async def on_paired(self, owner: UUID, device: Device) -> None:
        if await self._repo.default_of(owner) is None:
            device.is_default = True

    async def on_set(self, owner: UUID, device: Device) -> None:
        current = await self._repo.default_of(owner)
        if current is not None and current.device_uuid != device.device_uuid:
            current.is_default = False
            # ★ 先把旧的写下去再设新的：部分唯一索引不允许同时有两台默认
            await self._repo.flush()
        device.is_default = True

    async def on_revoked(self, owner: UUID, device: Device) -> None:
        if not device.is_default:
            return
        device.is_default = False
        await self._repo.flush()
        rest = [d for d in await self._repo.list(owner) if d.device_uuid != device.device_uuid]
        if rest:
            rest[0].is_default = True  # list 按最近连过排


class DeviceService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        redis: aioredis.Redis | None = None,
        pairing_ttl_s: int = 600,
    ) -> None:
        self._repo = DeviceRepository(session)
        self._defaults = DefaultDevicePolicy(self._repo)
        self._codes = PairingCodes(redis, pairing_ttl_s) if redis is not None else None

    # ------------------------------------------------------------------ 配对

    async def start_pairing(self, owner: UUID) -> PairingCode:
        assert self._codes is not None, "配对要 Redis"
        return await self._codes.issue(owner)

    async def redeem(self, code: str, report: dict[str, Any]) -> IssuedDevice | None:
        """码有效就建设备、发令牌；无效或已用过返回 None。"""
        assert self._codes is not None, "配对要 Redis"
        owner = await self._codes.take(code)
        if owner is None:
            return None
        raw, digest = DeviceToken.issue()
        now = datetime.now(UTC)
        device = Device(
            owner_uuid=owner,
            name=_name_of(report),
            token_hash=digest,
            paired_at=now,
        )
        _apply_report(device, report)
        self._repo.add(device)
        await self._repo.flush()
        await self._defaults.on_paired(owner, device)
        await self._repo.flush()
        return IssuedDevice(DeviceIdentity(device.device_uuid, owner, device.name), raw)

    async def authenticate(self, token: str, report: dict[str, Any]) -> DeviceIdentity | None:
        """按摘要找设备；已吊销或找不到一律拒绝。通过后记下最近一次连上的时间、系统和连接器版本。"""
        device = await self._repo.by_token_hash(DeviceToken.digest(token))
        if device is None or device.status != "active":
            return None
        device.last_seen_at = datetime.now(UTC)
        _apply_report(device, report)
        await self._repo.flush()
        return DeviceIdentity(device.device_uuid, device.owner_uuid, device.name)

    # ------------------------------------------------------------------ 我的设备

    async def list(self, owner: UUID) -> list[Device]:
        return await self._repo.list(owner)

    async def get(self, owner: UUID, device_uuid: UUID) -> Device:
        device = await self._repo.get(device_uuid)
        if device is None or device.owner_uuid != owner or device.status != "active":
            raise NotFound(f"设备 {device_uuid} 不存在", device_id=str(device_uuid))
        return device

    async def default_of(self, owner: UUID) -> Device | None:
        return await self._repo.default_of(owner)

    async def rename(self, owner: UUID, device_uuid: UUID, name: str) -> Device:
        device = await self.get(owner, device_uuid)
        device.name = name.strip()[:_NAME_MAX] or device.name
        await self._repo.flush()
        return device

    async def set_default(self, owner: UUID, device_uuid: UUID) -> Device:
        device = await self.get(owner, device_uuid)
        await self._defaults.on_set(owner, device)
        await self._repo.flush()
        return device

    async def revoke(self, owner: UUID, device_uuid: UUID) -> DeviceRevoked:
        device = await self.get(owner, device_uuid)
        now = datetime.now(UTC)
        device.status, device.revoked_at = "revoked", now
        await self._defaults.on_revoked(owner, device)
        await self._repo.flush()
        return DeviceRevoked(device.device_uuid, owner, now)


def _name_of(report: dict[str, Any]) -> str:
    name = str(report.get("hostname") or "").strip()
    return name[:_NAME_MAX] or "我的电脑"


def _apply_report(device: Device, report: dict[str, Any]) -> None:
    if report.get("os"):
        device.os = str(report["os"])[:64]
    if report.get("connector_version"):
        device.connector_version = str(report["connector_version"])[:32]
