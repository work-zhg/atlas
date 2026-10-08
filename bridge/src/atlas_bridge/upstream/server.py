"""UpstreamServer：监听、握手鉴权、健康检查、连接取代（Bridge 设计 §4.3 · §8.2 · §8.5）。

握手前（``process_request``）按顺序检查，全部在升级之前完成：
  /healthz、/readyz  → 直接回 HTTP，不升级（健康检查不在日志里留下连接噪音）
  路径               → 只有 /host 可以升级，其余 404
  token              → 常数时间比较，失败 401
  X-Atlas-Session    → 必须等于本 Pod 的会话，失败 403
  子协议             → server 报出的版本里没有 bridge 支持的，426
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from http import HTTPStatus

from atlas_host import HEADER_SESSION, SUBPROTOCOL_V1, CloseCode
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from ..session.host import HostSession, Phase
from .connection import UpstreamConnection
from .outbox import Outbox

__all__ = ["UpstreamServer"]

logger = logging.getLogger(__name__)

PATH = "/host"


class UpstreamServer:
    def __init__(
        self,
        host: HostSession,
        outbox: Outbox,
        *,
        token: str,
        session_id: str,
        listen_host: str,
        listen_port: int,
        max_message_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._host = host
        self._outbox = outbox
        self._token = token.encode()
        self._session_id = session_id
        self._listen = (listen_host, listen_port)
        self._max_message_bytes = max_message_bytes
        self._server: Server | None = None
        #: 当前连接。一个会话同一时刻只有一条（§4.3）
        self.current: UpstreamConnection | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]  # type: ignore[no-any-return]

    async def start(self) -> None:
        self._server = await serve(
            self._handler,
            *self._listen,
            subprotocols=[SUBPROTOCOL_V1],  # type: ignore[list-item]
            process_request=self._process_request,
            compression=None,  # 集群内带宽充足，压缩只增加 CPU 与内存
            max_size=self._max_message_bytes,
            server_header=None,
        )
        logger.info("上游端口已监听：%s:%d", self._listen[0], self.port)

    async def stop(self, code: int = CloseCode.GOING_AWAY, reason: str = "") -> None:
        if self.current is not None:
            await self.current.close(code, reason)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------ 握手

    def _process_request(self, conn: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == "/healthz":
            return conn.respond(HTTPStatus.OK, "ok\n")
        if path == "/readyz":
            ready = self._host.phase is not Phase.BOOTING
            status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
            return conn.respond(status, f"{self._host.phase.value}\n")
        if path != PATH:
            return conn.respond(HTTPStatus.NOT_FOUND, "not found\n")

        headers = request.headers
        if not self._token_ok(headers):
            logger.warning("上游握手鉴权失败，来源 %s", conn.remote_address)
            return conn.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        if headers.get(HEADER_SESSION) != self._session_id:
            logger.warning("上游握手的会话不匹配：%r", headers.get(HEADER_SESSION))
            return conn.respond(HTTPStatus.FORBIDDEN, "session mismatch\n")
        offered = [
            p.strip()
            for value in headers.get_all("Sec-WebSocket-Protocol")
            for p in value.split(",")
        ]
        if SUBPROTOCOL_V1 not in offered:
            return conn.respond(HTTPStatus.UPGRADE_REQUIRED, f"supported: {SUBPROTOCOL_V1}\n")
        return None

    def _token_ok(self, headers: Headers) -> bool:
        auth = headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        return hmac.compare_digest(token.strip().encode(), self._token)

    # ------------------------------------------------------------------ 连接

    async def _handler(self, ws: ServerConnection) -> None:
        conn = UpstreamConnection(ws, self._host, self._outbox)
        if self._host.ended.is_set():
            await conn.close(CloseCode.SESSION_GONE, "session ended")
            return
        previous, self.current = self.current, conn
        if previous is not None:
            logger.info("新的上游连接取代了旧连接")
            asyncio.create_task(previous.close(CloseCode.SUPERSEDED, "superseded"))  # noqa: RUF006
        self._outbox.bind(conn)
        self._host.upstream_changed(True)
        logger.info("上游已连接：%s", conn.remote)
        try:
            await conn.run()
        finally:
            if self.current is conn:
                self.current = None
                self._outbox.bind(None)
                self._host.upstream_changed(False)
            logger.info("上游连接断开：%s", conn.remote)

    async def close_current(self, code: int, reason: str) -> None:
        if self.current is not None:
            await self.current.close(code, reason)

    async def close_current_after_drain(self, code: int, reason: str) -> None:
        if self.current is not None:
            await self.current.close_after_drain(code, reason)
