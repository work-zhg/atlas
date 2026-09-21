"""BridgeServer —— Pod 内的 WS 端点。

Bridge 对 server 是**透明管道加三件本地事务**：

    ① 握手鉴权     每 Pod 一份凭证 + 会话绑定校验
    ② 单活跃 prompt 第二个 prompt 直接拒，不排队
    ③ 优雅终止     见 lifecycle.py

其余帧原样在 WS 与 adapter 的 stdio 之间搬运 —— 语义归两端。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from atlas_acp.wire import ErrorCode, Method, is_notification
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from atlas_bridge.adapter import AdapterCrashed, AdapterProcess

logger = logging.getLogger(__name__)

__all__ = ["CLOSE_SUPERSEDED", "CLOSE_UNAUTHORIZED", "BridgeServer"]

#: 握手不通过。★ 不回 200 再在应用层说"无权"—— 拒在握手上，
#: 扫描者连一个可用的 WS 通道都拿不到。
CLOSE_UNAUTHORIZED = 4401

#: 已有连接被新连接取代。
#:
#: ★ 为什么是"取代"而不是"拒绝新连接"：server 重启后会重连，而旧连接的
#:   TCP 可能要几分钟才被判定死亡。拒新的话 Pod 在这段时间里完全不可达 ——
#:   而这恰恰是最需要它可达的时刻。两个连接都过了同一份 token 校验，
#:   来源是同一个 server，让新的赢是安全的。
CLOSE_SUPERSEDED = 4409

#: 单帧上限，与 adapter 侧同口径。
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class BridgeServer:
    """一个 Pod 一个实例。

    ★ 它不认识 atlas_server —— 与 server 的唯一接口是 WS 上的线协议
      （atlas_acp）。代码级共享会让「升级 server 必须同步升级全部在跑的
      Pod」，而 Pod 在跑时没法原地升级（acp 详设 §03）。
    """

    def __init__(
        self,
        adapter: AdapterProcess,
        *,
        token: str,
        thread_id: str,
        request_timeout_s: float = 300.0,
    ) -> None:
        self._adapter = adapter
        self._token = token
        self._thread_id = thread_id
        self._request_timeout_s = request_timeout_s
        self._conn: ServerConnection | None = None
        #: ② 单活跃 prompt 的门。持有 = 有一轮在跑。
        self._prompt_busy = False
        #: adapter 反向请求（request_permission）在等 server 回话。
        self._pending_reverse: dict[int, asyncio.Future[Any]] = {}
        self._next_reverse_id = 0
        self._draining = False

    # ------------------------------------------------------------------ 对外

    async def serve_forever(self, host: str, port: int) -> None:
        async with serve(
            self._handle,
            host,
            port,
            max_size=_MAX_MESSAGE_BYTES,
            # 应用层心跳：LB / 网关的空闲超时会悄悄断长连接，表现为
            # 「CLI 没反应」（acp 详设 §13）。ping 让断连立刻可见。
            ping_interval=20,
            ping_timeout=20,
        ):
            await asyncio.Future()

    def begin_drain(self) -> None:
        """优雅终止第一步：拒绝新请求（lifecycle.py 调用）。"""
        self._draining = True

    # ------------------------------------------------------------------ ① 握手

    def _authorized(self, conn: ServerConnection) -> bool:
        """★ K8s 的默认网络是平的：集群里任何 Pod 都能连任何 Pod 的端口。

        不校验的话，同集群的其他工作负载 —— 包括别的租户的会话 Pod ——
        就能连上来驱动 CLI：读该会话的工作区、用该会话的凭证执行命令。
        「它在 Pod 网络里」不是一种安全边界（执行环境 §09）。
        """
        headers = conn.request.headers if conn.request is not None else {}
        token = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        thread_id = (headers.get("X-Thread-Id") or "").strip()
        if not token or token != self._token:
            return False
        # 会话绑定：本 Pod 只接受针对**它所属会话**的指令。
        return thread_id == self._thread_id

    async def _handle(self, conn: ServerConnection) -> None:
        if not self._authorized(conn):
            logger.warning("拒绝未授权的 WS 握手")
            await conn.close(CLOSE_UNAUTHORIZED, "unauthorized")
            return

        previous = self._conn
        self._conn = conn
        if previous is not None:
            # 新连接取代旧的（见 CLOSE_SUPERSEDED 的论证）。
            with contextlib.suppress(Exception):
                await previous.close(CLOSE_SUPERSEDED, "superseded")

        inflight: set[asyncio.Task[None]] = set()
        try:
            async for raw in conn:
                # ★ 每帧起一个任务，**不能**在循环里直接 await。
                #
                #   顺序处理会让整轮 prompt 堵住消息循环，后果是三件事同时坏：
                #     · 单活跃 prompt 的门永远触发不了（第二个 prompt 根本
                #       读不到，只是在传输层排队）
                #     · session/cancel 送不进去 —— 正在跑的那一轮取消不掉
                #     · 权限回传直接死锁：bridge 在等 adapter，adapter 在等
                #       用户决定，而决定就堵在这条读不到的消息里
                task = asyncio.create_task(self._on_message(conn, raw))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
        except ConnectionClosed:
            pass
        finally:
            for task in tuple(inflight):
                task.cancel()
            if self._conn is conn:
                self._conn = None

    # ------------------------------------------------------------------ 帧分流

    async def _on_message(self, conn: ServerConnection, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            await self._send(conn, _error(None, ErrorCode.PARSE_ERROR, "帧不是合法 JSON"))
            return
        if not isinstance(frame, dict):
            await self._send(conn, _error(None, ErrorCode.INVALID_REQUEST, "帧不是对象"))
            return

        # server 回的是**反向请求的响应**（request_permission 的决定）
        if "method" not in frame:
            self._resolve_reverse(frame)
            return

        if is_notification(frame):
            await self._forward_notification(frame)
            return

        await self._serve_request(conn, frame)

    async def _forward_notification(self, frame: dict[str, Any]) -> None:
        """server → adapter 的通知。目前只有 session/cancel。

        ★ 取消**不受 draining 与 busy 门限制**：正在收尾时更要让它能停。
        """
        with contextlib.suppress(AdapterCrashed):
            await self._adapter.notify(frame["method"], frame.get("params") or {})

    async def _serve_request(self, conn: ServerConnection, frame: dict[str, Any]) -> None:
        frame_id = frame.get("id")
        method = frame.get("method", "")

        if self._draining:
            await self._send(
                conn, _error(frame_id, ErrorCode.INTERNAL_ERROR, "bridge 正在终止，拒绝新请求")
            )
            return

        # ② 单活跃 prompt。
        #
        # ★ 回 JSON-RPC 错误而**不是**关连接：关掉会把第一轮正在流式输出的
        #   session/update 一起掐断 —— 第二个请求的错误不该毁掉第一个的成果。
        #   排队也不行：会话串行锁的超时是按一轮算的，排队会让它变得不可解释
        #   （执行环境 §03）。
        if method == Method.SESSION_PROMPT:
            if self._prompt_busy:
                await self._send(
                    conn,
                    _error(frame_id, ErrorCode.SESSION_BUSY, "已有活跃的 prompt，同会话必须串行"),
                )
                return
            self._prompt_busy = True
            try:
                await self._relay_request(conn, frame_id, method, frame.get("params") or {})
            finally:
                self._prompt_busy = False
            return

        await self._relay_request(conn, frame_id, method, frame.get("params") or {})

    async def _relay_request(
        self, conn: ServerConnection, frame_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        try:
            result = await self._adapter.request(method, params, timeout=self._request_timeout_s)
        except AdapterCrashed as exc:
            await self._send(conn, _error(frame_id, ErrorCode.INTERNAL_ERROR, str(exc)))
            return
        except TimeoutError:
            await self._send(conn, _error(frame_id, ErrorCode.INTERNAL_ERROR, "adapter 响应超时"))
            return
        await self._send(conn, {"jsonrpc": "2.0", "id": frame_id, "result": result})

    # ------------------------------------------------------------------ adapter → server

    async def on_adapter_notification(self, frame: dict[str, Any]) -> None:
        """adapter 的通知（session/update 流）原样转给 server。"""
        await self._push(frame)

    async def on_adapter_request(self, frame: dict[str, Any]) -> Any:
        """adapter 的反向请求 —— 唯一的是 session/request_permission。

        ★ 原样转交 server，等用户的决定。**默认必须是转交，不是自动 allow**：
          一旦为了省事在这里写死自动批准，前端那套审批卡片就成了摆设，
          任意文件写入与命令执行的权限实际上交给了模型
          （执行环境 §02 的红线）。
        """
        conn = self._conn
        if conn is None:
            # 没有 server 连着就没人能批准 —— 当作拒绝，不是放行。
            msg = "无 server 连接，无法转交审批"
            raise AdapterCrashed(msg)

        self._next_reverse_id += 1
        reverse_id = self._next_reverse_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_reverse[reverse_id] = future
        try:
            await self._send(
                conn,
                {
                    "jsonrpc": "2.0",
                    "id": reverse_id,
                    "method": frame.get("method", Method.SESSION_REQUEST_PERMISSION),
                    "params": frame.get("params") or {},
                },
            )
            return await future
        finally:
            self._pending_reverse.pop(reverse_id, None)

    def _resolve_reverse(self, frame: dict[str, Any]) -> None:
        future = self._pending_reverse.pop(frame.get("id"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if frame.get("error") is not None:
            future.set_exception(AdapterCrashed(str(frame["error"])))
        else:
            future.set_result(frame.get("result"))

    # ------------------------------------------------------------------ 出站

    async def _push(self, frame: dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            # 断线期间的 update 直接丢弃：server 重连后会 session/load 拿回
            # 上下文，补发半截的流反而会让事件序乱掉（acp 详设 §10）。
            logger.debug("无 server 连接，丢弃 %s", frame.get("method"))
            return
        await self._send(conn, frame)

    async def _send(self, conn: ServerConnection, frame: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionClosed):
            await conn.send(json.dumps(frame, ensure_ascii=False))


def _error(frame_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": frame_id, "error": {"code": code, "message": message}}
