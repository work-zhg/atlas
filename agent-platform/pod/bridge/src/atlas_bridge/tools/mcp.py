"""PlatformMcp：把平台工具（request_takeover……）以 MCP 的形式提供给 CLI。

只听 127.0.0.1、带随机令牌；session.open 里 server 给了平台工具才把它放进 CLI 的 mcpServers
（名字 atlas，CLI 里的工具名形如 mcp__atlas__request_takeover）。工具调用经上游的 tool.call 交给
server，在线等结果 —— 和 CLI 的权限请求一样。

只实现 MCP streamable HTTP 里用得到的一小部分，无状态：
    POST /mcp   initialize · notifications/* · ping · tools/list · tools/call
    其余        405

★ tools/call 用 SSE 回：先回响应头，等的时候每 15 秒发一行注释（有 progressToken 再发一条进度）。
  接管可能等很久，CLI 的 HTTP 客户端在没有数据的连接上会超时断开。
★ 为什么不用 Shell 命令：Claude Code 的 Shell 最长 10 分钟，接管却不设期限（design-browser-device
  原理 4）。CLI 一侧的两个时限由 bridge 拉起 CLI 时放开（app.py）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from atlas_host import PlatformTool

__all__ = ["MCP_NAME", "PlatformMcp"]

logger = logging.getLogger(__name__)

#: CLI 里的服务名：工具名是 mcp__atlas__<工具>
MCP_NAME = "atlas"
PATH = "/mcp"
KEEPALIVE_S = 15.0
MAX_BODY = 4 * 1024 * 1024
#: Claude Code 在 tools/call 的 _meta 里带上这次调用的 tool_use id
TOOL_USE_META = "claudecode/toolUseId"

#: (工具名, 参数, 调用 id) → ToolCallResult 的线上形态 {content, isError}
CallTool = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]


class PlatformMcp:
    def __init__(
        self,
        tools: Callable[[], list[PlatformTool]],
        call: CallTool,
        *,
        version: str = "0",
        keepalive_s: float = KEEPALIVE_S,
    ) -> None:
        self._tools = tools
        self._call = call
        self._version = version
        self._keepalive_s = keepalive_s
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        logger.info("平台工具的 MCP 已监听：127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    def entry(self) -> dict[str, Any]:
        """放进 CLI 的 mcpServers（ACP 的格式：headers 是数组）。"""
        return {
            "type": "http",
            "name": MCP_NAME,
            "url": f"http://127.0.0.1:{self.port}{PATH}",
            "headers": [{"name": "Authorization", "value": f"Bearer {self._token}"}],
        }

    # ------------------------------------------------------------------ HTTP

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, headers, body = await _read_request(reader)
            if path.split("?", 1)[0] != PATH:
                await _respond(writer, 404, b"not found\n", "text/plain")
                return
            if not self._authorized(headers):
                await _respond(writer, 401, b"unauthorized\n", "text/plain")
                return
            if method != "POST":
                await _respond(writer, 405, b"method not allowed\n", "text/plain")
                return
            try:
                message = json.loads(body or b"null")
            except ValueError:
                await _json(writer, 400, _error(None, -32700, "不是 JSON"))
                return
            await self._dispatch(writer, message)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def _authorized(self, headers: dict[str, str]) -> bool:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip().encode(), self._token.encode()
        )

    # ------------------------------------------------------------------ JSON-RPC

    async def _dispatch(self, writer: asyncio.StreamWriter, message: Any) -> None:
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "id" in message:  # 客户端回的响应：没有要回的
                await _respond(writer, 202, b"", "text/plain")
                return
            await _json(writer, 400, _error(None, -32600, "不是 JSON-RPC 请求"))
            return
        method, msg_id = message["method"], message.get("id")
        if msg_id is None:  # 通知（notifications/initialized、cancelled……）
            await _respond(writer, 202, b"", "text/plain")
            return
        params = _obj(message.get("params"))
        if method == "initialize":
            result = {
                "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_NAME, "version": self._version},
            }
            await _json(writer, 200, _ok(msg_id, result))
        elif method == "ping":
            await _json(writer, 200, _ok(msg_id, {}))
        elif method == "tools/list":
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self._tools()
            ]
            await _json(writer, 200, _ok(msg_id, {"tools": tools}))
        elif method == "tools/call":
            await self._call_tool(writer, msg_id, params)
        else:
            await _json(writer, 200, _error(msg_id, -32601, f"不支持的方法：{method}"))

    async def _call_tool(
        self, writer: asyncio.StreamWriter, msg_id: Any, params: dict[str, Any]
    ) -> None:
        name = str(params.get("name") or "")
        if name not in {t.name for t in self._tools()}:
            await _json(writer, 200, _error(msg_id, -32602, f"没有这个工具：{name}"))
            return
        arguments = _obj(params.get("arguments"))
        meta = _obj(params.get("_meta"))
        call_id = str(meta.get(TOOL_USE_META) or "") or f"mcp-{uuid.uuid4().hex}"
        progress = meta.get("progressToken")

        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        call = asyncio.ensure_future(self._call(name, arguments, call_id))
        ticks = 0
        try:
            while True:
                done, _ = await asyncio.wait({call}, timeout=self._keepalive_s)
                if done:
                    break
                ticks += 1
                writer.write(b": keepalive\n\n")
                if progress is not None:
                    note = {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progressToken": progress, "progress": ticks},
                    }
                    writer.write(_event(note))
                await writer.drain()
        except (ConnectionError, OSError):
            # CLI 断开了这次调用：取消等待（broker 据此告诉 server 作废）
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await call
            return
        try:
            result = call.result()
        except Exception as exc:
            logger.exception("平台工具 %s 出错", name)
            result = {"content": f"平台工具出错：{exc}", "isError": True}
        payload = {
            "content": [{"type": "text", "text": str(result.get("content") or "")}],
            "isError": bool(result.get("isError")),
        }
        writer.write(_event(_ok(msg_id, payload)))
        await writer.drain()


# ---------------------------------------------------------------------------- HTTP 小工具


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length > MAX_BODY:
        raise ValueError("请求体太大")
    body = await reader.readexactly(length) if length else b""
    return method.upper(), path, headers, body


async def _respond(writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str) -> None:
    reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized"}.get(
        status, {404: "Not Found", 405: "Method Not Allowed"}.get(status, "OK")
    )
    head = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)
    await writer.drain()


async def _json(writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
    await _respond(
        writer, status, json.dumps(payload, ensure_ascii=False).encode(), "application/json"
    )


def _event(payload: dict[str, Any]) -> bytes:
    return b"event: message\ndata: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
