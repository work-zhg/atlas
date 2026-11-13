"""设备网关的内部接口（c4-model/agent/device/code-device-session.html · DeviceGateway）。

    presence      查在线：在线表里有这台设备，就有它连在哪个网关实例上
    register      登记本轮能力（真实上游和启动参数），换回会话令牌和设备视角的地址
    host_url      这个会话在设备所在实例上的地址（Agent 服务的上游连接连这里）
    revoke        作废会话登记
    disconnect    吊销：断开设备、作废它上面所有会话的登记
    files         读本机文件（列改动、列目录、列允许目录、取文件）

★ 连会话、读文件都直连设备所在的那个网关实例（在线表里的 instance）：一台设备只有一条控制连接，
  它的会话流和文件流都在那个实例上，网关实例之间不转发。登记存在共用的 Redis 里，哪个实例都行。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings

__all__ = [
    "DeviceGateway",
    "DeviceStatus",
    "FileResponse",
    "GatewayUnavailable",
    "SessionGrant",
    "Upstream",
]

logger = logging.getLogger(__name__)


class GatewayUnavailable(RuntimeError):
    """连不上设备网关，或网关回了意外的错误。"""


@dataclass(frozen=True)
class DeviceStatus:
    """在线的设备：连在哪个网关实例上，以及连接器上线时报告的情况。"""

    device_uuid: str
    instance: str
    name: str = ""
    #: 设备主人（核对设备令牌时写下的）
    owner: str = ""
    #: os · hostname · connector_version · clis（CLI 名 → 版本）· policy（本机策略摘要）
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def clis(self) -> dict[str, str]:
        clis = self.report.get("clis")
        return clis if isinstance(clis, dict) else {}

    @property
    def allowed_roots(self) -> list[str]:
        policy = self.report.get("policy")
        roots = policy.get("allowed_roots") if isinstance(policy, dict) else None
        return [str(r) for r in roots] if isinstance(roots, list) else []

    @property
    def remote_files(self) -> bool:
        policy = self.report.get("policy")
        return bool(policy.get("remote_files", True)) if isinstance(policy, dict) else True


@dataclass(frozen=True)
class Upstream:
    """登记给设备网关的一个真实上游：CLI 调 {网关}/u/{id}/… 时，网关换上这里的请求头转发过去。"""

    id: str
    kind: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionGrant:
    """登记的回执：会话令牌（整个 CLI 会话不变）和每个上游在设备视角的地址。"""

    token: str
    endpoints: dict[str, str]


@dataclass
class FileResponse:
    """读本机文件的响应：状态、响应头、JSON 体（列改动、列目录）或内容流（取文件）。"""

    status: int
    headers: dict[str, str]
    body: Any = None
    stream: AsyncIterator[bytes] | None = None
    _response: httpx.Response | None = None

    async def aclose(self) -> None:
        if self._response is not None:
            await self._response.aclose()


class DeviceGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = (settings.device_gateway_url or "").rstrip("/")
        token = settings.device_gateway_token
        self._token = token.get_secret_value() if token is not None else ""
        self._timeout = settings.device_gateway_timeout_s
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base)

    # ------------------------------------------------------------------ 在线、登记

    async def presence(self, device_uuid: str) -> DeviceStatus | None:
        resp = await self._request("GET", f"{self._base}/internal/presence/{device_uuid}")
        if resp.status_code == 404:
            return None
        body = _json(resp)
        return DeviceStatus(
            device_uuid=str(body.get("device") or device_uuid),
            instance=str(body.get("instance") or self._base),
            name=str(body.get("name") or ""),
            owner=str(body.get("owner") or ""),
            report=body.get("report") if isinstance(body.get("report"), dict) else {},
        )

    async def register(
        self,
        thread_uuid: str,
        *,
        device_uuid: str,
        owner_uuid: str,
        launch: dict[str, Any],
        upstreams: list[Upstream],
    ) -> SessionGrant:
        body = {
            "device": device_uuid,
            "owner": owner_uuid,
            "launch": launch,
            "upstreams": [
                {"id": u.id, "kind": u.kind, "url": u.url, "headers": u.headers} for u in upstreams
            ],
        }
        resp = await self._request(
            "PUT", f"{self._base}/internal/sessions/{thread_uuid}", json=body
        )
        data = _json(resp)
        return SessionGrant(token=str(data["token"]), endpoints=dict(data.get("endpoints") or {}))

    def host_url(self, status: DeviceStatus, thread_uuid: str) -> tuple[str, str]:
        """→ (这个会话在设备所在实例上的 ws 地址, 服务令牌)。"""
        parsed = urlparse(status.instance)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/internal/host/{thread_uuid}"
        return url, self._token

    async def revoke(self, thread_uuid: str) -> None:
        resp = await self._request("DELETE", f"{self._base}/internal/sessions/{thread_uuid}")
        if resp.status_code not in (200, 204, 404):
            raise GatewayUnavailable(f"作废会话登记失败（{resp.status_code}）")

    async def disconnect(self, device_uuid: str, reason: str) -> None:
        """在设备所在的实例上断开（不在线时只作废会话登记）。"""
        status = await self.presence(device_uuid)
        base = status.instance.rstrip("/") if status is not None else self._base
        await self._request(
            "POST", f"{base}/internal/devices/{device_uuid}/disconnect", json={"reason": reason}
        )

    # ------------------------------------------------------------------ 本机文件

    async def files(self, status: DeviceStatus, op: str, **params: Any) -> FileResponse:
        """在设备所在的实例上开一条文件流。fetch 返回内容流，调用方负责 aclose。"""
        body = {"op": op, **{k: v for k, v in params.items() if v is not None}}
        url = f"{status.instance.rstrip('/')}/internal/files/{status.device_uuid}"
        client = self._client(timeout=httpx.Timeout(self._timeout, read=None))
        try:
            request = client.build_request("POST", url, json=body, headers=self._headers())
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise GatewayUnavailable(f"连不上设备网关：{exc}") from exc
        headers = dict(resp.headers)
        if op == "fetch" and resp.status_code in (200, 206):

            async def stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in resp.aiter_raw():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return FileResponse(resp.status_code, headers, stream=stream(), _response=resp)
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        return FileResponse(resp.status_code, headers, body=_decode(raw))

    # ------------------------------------------------------------------ 内部

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _client(self, *, timeout: float | httpx.Timeout | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout if timeout is not None else self._timeout,
            transport=self._transport,
            # ★ 不走代理：设备网关在集群内（同 cli/client.py 的说明）
            trust_env=False,
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not self._base:
            raise GatewayUnavailable("这个部署没有设备网关（DEVICE_GATEWAY_URL 未配置）")
        try:
            async with self._client() as client:
                resp = await client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"连不上设备网关：{exc}") from exc
        if resp.status_code >= 500:
            raise GatewayUnavailable(f"设备网关返回 {resp.status_code}：{resp.text[:200]}")
        return resp


def _json(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise GatewayUnavailable(f"设备网关返回 {resp.status_code}：{resp.text[:200]}")
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _decode(raw: bytes) -> Any:
    """列改动、列目录和各种错误是 JSON；取文件不成功时网关只给一段文字。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")
