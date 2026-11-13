"""控制连接（c4-model/agent/connector/code-connector-link.html）。

    Connector      配对；连上设备网关并保持，断了退避重连（不设上限），被拒才停
    ReportBuilder  连上时的报告：系统、版本、装了哪些 CLI、本机策略
    ChannelRouter  按通道类型分发：会话流 → 本机会话，文件流 → 本机文件

★ 连接只由电脑发起：电脑在路由器、防火墙后面，平台连不进来；访问 HTTPS 入口几乎处处可行。
★ 遵守系统的代理设置（websockets 默认读系统代理）：公司网络常常只能经代理出网。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import random
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from atlas_devicelink import FILE, SESSION, Channel, Mux
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from . import __version__
from .config import LocalPolicy
from .credentials import DeviceCredential

__all__ = ["Backoff", "Connector", "Fatal", "ReportBuilder", "gateway_url", "pair"]

logger = logging.getLogger(__name__)

#: 网关的关闭码：这些说明重连也没用，停下来请人处理
FATAL = {
    4401: "设备令牌无效或设备已吊销：请在网页上重新获取配对码，运行 atlas-connect pair",
    4426: "连接器版本太旧：请升级（atlas-connect upgrade）",
    4409: "这台设备在别处连上了：另一个连接器在用同一份凭据",
}


class Fatal(Exception):
    """被网关拒绝：重连也没用。"""


def gateway_url(server: str) -> str:
    """平台地址 → 设备网关的地址（公网入口 + /device-api）。

    开发时可以用 --gateway 直接给网关地址。
    """
    return server.rstrip("/") + "/device-api"


_WS_SCHEME = {"https": "wss", "http": "ws"}


def _ws(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        parts._replace(scheme=_WS_SCHEME.get(parts.scheme, parts.scheme))
    )


class ReportBuilder:
    def __init__(self, policy: LocalPolicy) -> None:
        self._policy = policy

    def build(self) -> dict[str, Any]:
        clis: dict[str, str] = {}
        for name, cli in self._policy.clis.items():
            exe = shutil.which(cli.command[0]) if cli.command else None
            if exe is None:
                continue
            clis[name] = _version_of(cli.command) or "已安装"
        return {
            "os": f"{platform.system()} {platform.release()}",
            "hostname": socket.gethostname(),
            "connector_version": __version__,
            "clis": clis,
            "policy": self._policy.summary(),
        }


def _version_of(command: tuple[str, ...]) -> str | None:
    with contextlib.suppress(Exception):
        out = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=5)
        text = (out.stdout or out.stderr).strip().splitlines()
        if out.returncode == 0 and text:
            return text[0][:40]
    return None


def pair(gateway: str, code: str, report: dict[str, Any]) -> DeviceCredential:
    """用配对码换设备令牌。失败抛 Fatal（码不对、过期）或 ConnectionError（连不上）。"""
    gw = gateway.rstrip("/")
    body = json.dumps({"code": code.strip().upper(), "report": report}).encode()
    req = urllib.request.Request(
        gw + "/pair", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            issued = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read())["error"]["message"]
        raise Fatal(detail or f"配对失败（{exc.code}）") from exc
    except OSError as exc:
        raise ConnectionError(f"连不上 {gw}：{exc}") from exc
    return DeviceCredential(
        gw, str(issued["device_uuid"]), str(issued.get("name") or ""), issued["token"]
    )


class Backoff:
    """0.5 秒起翻倍、上限 30 秒、带抖动，不设总时限。"""

    MAX_S = 30.0

    def __init__(self) -> None:
        self._delay = 0.5

    def next_delay(self) -> float:
        delay = self._delay * random.uniform(0.8, 1.2)
        self._delay = min(self._delay * 2, self.MAX_S)
        return delay

    def reset(self) -> None:
        self._delay = 0.5


class _Transport:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    async def send(self, message: str | bytes) -> None:
        await self._ws.send(message)

    async def receive(self) -> str | bytes | None:
        try:
            return await self._ws.recv()
        except ConnectionClosed:
            return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code, reason)


OnChannel = Callable[[Channel], Any]


class Connector:
    """state：connecting / online / backoff / stopped。"""

    def __init__(
        self,
        cred: DeviceCredential,
        report: ReportBuilder,
        *,
        on_session: OnChannel,
        on_file: OnChannel,
    ) -> None:
        self._cred = cred
        self._report = report
        self._routes = {SESSION: on_session, FILE: on_file}
        self._backoff = Backoff()
        self._ws: ClientConnection | None = None
        self._stopping = False
        self.state = "connecting"
        self.connected_at: float | None = None

    async def run(self) -> None:
        """连上、保持；断了退避重连。被拒抛 Fatal；stop() 后返回。"""
        while not self._stopping:
            self.state = "connecting"
            try:
                await self._session()
            except Fatal:
                self.state = "stopped"
                raise
            except Exception as exc:
                logger.warning("和设备网关的连接断了：%s", exc)
            if self._stopping:
                break
            self.state = "backoff"
            await asyncio.sleep(self._backoff.next_delay())
        self.state = "stopped"

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close(1001, "连接器退出")

    async def _session(self) -> None:
        url = _ws(self._cred.gateway) + "/connect"
        ws = await connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self._cred.token}",
                "X-Connector-Version": __version__,
            },
            max_size=64 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
        )
        self._ws = ws
        try:
            await ws.send(json.dumps({"t": "hello", "report": self._report.build()}))
            try:
                welcome = json.loads(await ws.recv())
            except ConnectionClosed as exc:
                self._raise_if_fatal(
                    exc.rcvd.code if exc.rcvd else None, exc.rcvd.reason if exc.rcvd else ""
                )
                raise
            logger.info("已连上设备网关：%s（%s）", welcome.get("name"), welcome.get("device"))
            self.state = "online"
            self.connected_at = asyncio.get_running_loop().time()
            self._backoff.reset()
            mux = Mux(_Transport(ws), opener=False, on_open=self._route)
            await mux.run()
            self._raise_if_fatal(ws.close_code, ws.close_reason or "")
        finally:
            self._ws = None
            with contextlib.suppress(Exception):
                await ws.close()

    def _raise_if_fatal(self, code: int | None, reason: str) -> None:
        if code in FATAL:
            raise Fatal(f"{FATAL[code]}（{reason}）" if reason else FATAL[code])

    async def _route(self, channel: Channel) -> None:
        handler = self._routes.get(channel.kind)
        if handler is None:
            await channel.close(4400, f"不认识的通道类型：{channel.kind}")
            return
        await handler(channel)
