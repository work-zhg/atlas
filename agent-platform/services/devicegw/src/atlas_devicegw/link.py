"""设备连接（c4-model/agent/devicegw/code-devicegw-link.html）。

    LinkServer   接住连接器的控制连接：查版本 → 核对令牌 → 本实例登记 → 写在线表；心跳续期
    DeviceLink   本实例上一台设备的连接，里面多路复用会话流和文件流
    DeviceAuth   Agent 服务内部接口的客户端：核对设备令牌、兑换配对码

握手（连接器 → 网关）：
    1. 带 Authorization: Bearer <设备令牌>、X-Connector-Version 连 /connect
    2. 第一条消息 {"t": "hello", "report": {...}}（系统、版本、装了哪些 CLI、允许的目录）
    3. 网关回 {"t": "welcome", "device": ..., "name": ...}，之后这条连接上只走多路复用的帧

关闭码（连接器据此决定要不要重连）：
    4401 令牌无效或设备已吊销  → 不重连，请重新配对
    4426 连接器版本太旧        → 不重连，请升级
    4409 被同一台设备的新连接取代 → 不重连（别处有一个连接器在用同一份凭据）
    1013 Agent 服务暂时问不到   → 稍后重连
    其他                       → 退避重连
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from atlas_devicelink import GOING_AWAY, Channel, Mux
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .store import PresenceTable

__all__ = [
    "AuthUnavailable",
    "DeviceAuth",
    "DeviceLink",
    "LinkServer",
    "StarletteTransport",
    "fit_reason",
]

logger = logging.getLogger(__name__)

UNAUTHORIZED = 4401
UPGRADE_REQUIRED = 4426
SUPERSEDED = 4409
TRY_AGAIN_LATER = 1013
HELLO_TIMEOUT_S = 10.0


def fit_reason(reason: str, limit: int = 123) -> str:
    """WebSocket 的关闭原因最多 123 字节（UTF-8）：按字符边界截短。完整原因另经消息送达。"""
    raw = reason.encode()
    if len(raw) <= limit:
        return reason
    return raw[: limit - 3].decode(errors="ignore") + "…"


class AuthUnavailable(RuntimeError):
    """Agent 服务暂时问不到。"""


class DeviceAuth:
    def __init__(self, agent_url: str, client: httpx.AsyncClient) -> None:
        self._base = agent_url.rstrip("/")
        self._client = client

    async def authenticate(self, token: str, report: dict[str, Any]) -> dict[str, Any] | None:
        return await self._post(
            "/internal/devices/authenticate", {"token": token, "report": report}
        )

    async def redeem(self, code: str, report: dict[str, Any]) -> dict[str, Any] | None:
        return await self._post("/internal/devices/redeem", {"code": code, "report": report})

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(self._base + path, json=body)
        except httpx.HTTPError as exc:
            raise AuthUnavailable(str(exc)) from exc
        if resp.status_code in (400, 401, 403, 404, 410):
            return None
        if resp.status_code >= 300:
            raise AuthUnavailable(f"Agent 服务返回 {resp.status_code}")
        return resp.json()  # type: ignore[no-any-return]


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split(".")[:3]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def version_ok(version: str, minimum: str) -> bool:
    return _version_tuple(version) >= _version_tuple(minimum)


class StarletteTransport:
    """把 Starlette 的 WebSocket 适配成多路复用要的 Transport。"""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._closed = False

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, str):
            await self._ws.send_text(message)
        else:
            await self._ws.send_bytes(message)

    async def receive(self) -> str | bytes | None:
        if self._closed:
            return None
        try:
            msg = await self._ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            self._closed = True
            return None
        if msg["type"] == "websocket.disconnect":
            self._closed = True
            return None
        text = msg.get("text")
        return text if text is not None else msg.get("bytes")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close(code, fit_reason(reason))


@dataclass
class DeviceLink:
    """本实例上一台设备的控制连接。"""

    device: str
    owner: str
    name: str
    link: str
    report: dict[str, Any]
    mux: Mux

    async def open_channel(self, kind: str, head: dict[str, Any]) -> Channel:
        return await self.mux.open(kind, head)

    async def supersede(self) -> None:
        """被同一台设备的新连接取代：上面的通道以「可以重连」关闭，Agent 服务重连后落到新连接上。"""
        self.mux.lost_code, self.mux.lost_reason = GOING_AWAY, "设备换了一条连接"
        await self.mux.close(SUPERSEDED, "被同一台设备的新连接取代")


class LinkServer:
    def __init__(
        self,
        presence: PresenceTable,
        auth: DeviceAuth,
        *,
        instance_url: str,
        min_version: str,
        presence_ttl_s: int,
        stall_s: float,
    ) -> None:
        self._presence = presence
        self._auth = auth
        self._instance = instance_url
        self._min_version = min_version
        self._ttl = presence_ttl_s
        self._stall_s = stall_s
        self._links: dict[str, DeviceLink] = {}

    def local(self, device: str) -> DeviceLink | None:
        link = self._links.get(device)
        return link if link is not None and not link.mux.done else None

    async def presence(self, device: str) -> dict[str, Any] | None:
        return await self._presence.get(device)

    async def pair(self, code: str, report: dict[str, Any]) -> dict[str, Any] | None:
        return await self._auth.redeem(code, report)

    async def disconnect(self, device: str, reason: str) -> bool:
        link = self.local(device)
        if link is None:
            return False
        await link.mux.close(UNAUTHORIZED, reason)
        return True

    async def accept(self, ws: WebSocket) -> None:
        await ws.accept()
        transport = StarletteTransport(ws)
        version = ws.headers.get("x-connector-version", "0")
        if not version_ok(version, self._min_version):
            await transport.close(
                UPGRADE_REQUIRED,
                f"连接器 {version} 版本太旧（最低 {self._min_version}），"
                "请运行 atlas-connect upgrade",
            )
            return
        token = _bearer(ws)
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
        except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
            await transport.close(4400, "握手后第一条消息应当是 hello")
            return
        report = hello.get("report") if isinstance(hello, dict) else None
        if not isinstance(report, dict):
            await transport.close(4400, "hello 里要有 report")
            return
        if token is None:
            await transport.close(UNAUTHORIZED, "缺少设备令牌")
            return
        try:
            identity = await self._auth.authenticate(
                token, {**report, "connector_version": version}
            )
        except AuthUnavailable as exc:
            logger.warning("核对设备令牌时 Agent 服务不可用：%s", exc)
            await transport.close(TRY_AGAIN_LATER, "平台暂时不可用，稍后重连")
            return
        if identity is None:
            await transport.close(UNAUTHORIZED, "设备令牌无效或设备已吊销，请重新配对")
            return
        device, link_id = str(identity["device_uuid"]), uuid.uuid4().hex
        link = DeviceLink(
            device=device,
            owner=str(identity["owner_uuid"]),
            name=str(identity.get("name") or ""),
            link=link_id,
            report=report,
            mux=Mux(transport, opener=True, stall_s=self._stall_s),
        )
        previous = self._links.get(device)
        self._links[device] = link
        if previous is not None:
            logger.info("设备 %s 换了一条连接，关闭旧的", device)
            await previous.supersede()
        await self._presence.put(
            {
                "device": device,
                "owner": link.owner,
                "name": link.name,
                "instance": self._instance,
                "link": link_id,
                "report": {**report, "connector_version": version},
                "connected_at": time.time(),
            }
        )
        await ws.send_text(json.dumps({"t": "welcome", "device": device, "name": link.name}))
        logger.info("设备 %s（%s）已连上", device, link.name)
        refresher = asyncio.create_task(self._keep_present(device, link_id))
        try:
            await link.mux.run()
        finally:
            refresher.cancel()
            if self._links.get(device) is link:
                del self._links[device]
            await self._presence.drop(device, link_id)
            logger.info("设备 %s 断开", device)

    async def _keep_present(self, device: str, link: str) -> None:
        while True:
            await asyncio.sleep(max(1.0, self._ttl / 3))
            with contextlib.suppress(Exception):
                await self._presence.touch(device, link)


def _bearer(ws: WebSocket) -> str | None:
    scheme, _, value = ws.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None
