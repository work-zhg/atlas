"""测试用的 Higress 替身：/mcp-servers/{名字} 上的最小 MCP 服务（Streamable HTTP，JSON-RPC）。

手写而不是用 MCPServer：测试要在运行中改工具定义（定义漂移）、让会话失效、改用 SSE 返回、
记下每个请求带了哪些请求头 —— 这些在真实的 MCP 服务实现里都不好控制。

★ 和真实 Higress 一样只认网关凭据（Authorization: Bearer <凭据>），open=True 的服务除外
  （演示「Higress 上漏开了认证」）。
★ 在后台线程里起 uvicorn（自己的 event loop），网关经真实的 HTTP 调它。
"""

from __future__ import annotations

import inspect
import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

#: (参数, 请求头) → 文本结果；可以是 async 函数。抛 ToolFailure = 工具执行失败（isError）
Handler = Callable[[dict[str, Any], dict[str, str]], Any]


class ToolFailure(Exception):
    """工具执行失败：作为 isError 的结果返回（业务错误要原样交给模型）。"""


@dataclass
class FakeService:
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)
    #: True = 不要求网关凭据（漏开了认证）
    open: bool = False
    #: True = 以 text/event-stream 回复
    sse: bool = False
    sessions: set[str] = field(default_factory=set)
    #: 每个请求：(JSON-RPC 方法或 HTTP 方法, 请求头)
    seen: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def tool(
        self,
        name: str,
        description: str,
        handler: Handler,
        *,
        properties: dict[str, Any] | None = None,
        read_only: bool = False,
    ) -> None:
        self.tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}},
            **({"annotations": {"readOnlyHint": True}} if read_only else {}),
        }
        self.handlers[name] = handler


@dataclass
class FakeHigress:
    credential: str
    services: dict[str, FakeService] = field(default_factory=dict)
    #: 被拒的请求（没带或带错网关凭据）
    rejected: list[str] = field(default_factory=list)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/mcp-servers/{name}", self.handle, methods=["GET", "POST", "DELETE"]),
                Route("/mcp-servers/{name}/", self.handle, methods=["GET", "POST", "DELETE"]),
            ]
        )

    async def handle(self, request: Request) -> Response:
        svc = self.services.get(request.path_params["name"])
        if svc is None:
            return JSONResponse({"error": "no such server"}, status_code=404)
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not svc.open and headers.get("authorization") != f"Bearer {self.credential}":
            self.rejected.append(request.path_params["name"])
            return JSONResponse({"error": "gateway credential required"}, status_code=401)
        if request.method == "GET":
            svc.seen.append(("GET", headers))
            return Response(status_code=405)
        if request.method == "DELETE":
            svc.seen.append(("DELETE", headers))
            svc.sessions.discard(headers.get("mcp-session-id", ""))
            return Response(status_code=200)
        msg = json.loads(await request.body())
        method = str(msg.get("method"))
        svc.seen.append((method, headers))
        if method == "initialize":
            sid = uuid.uuid4().hex
            svc.sessions.add(sid)
            result = {
                "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": request.path_params["name"], "version": "1"},
            }
            return self._reply(svc, msg["id"], result, {"mcp-session-id": sid})
        if headers.get("mcp-session-id") not in svc.sessions:
            # 会话失效（服务重启、会话过期）：协议规定回 404
            return JSONResponse({"error": "session not found"}, status_code=404)
        if "id" not in msg:
            return Response(status_code=202)
        if method == "tools/list":
            return self._reply(svc, msg["id"], {"tools": list(svc.tools.values())})
        if method == "tools/call":
            name = msg["params"]["name"]
            handler = svc.handlers.get(name)
            if handler is None:
                text, error = f"unknown tool {name}", True
            else:
                try:
                    out = handler(msg["params"].get("arguments") or {}, headers)
                    text = await out if inspect.isawaitable(out) else out
                    error = False
                except ToolFailure as exc:
                    text, error = str(exc), True
            return self._reply(
                svc, msg["id"], {"content": [{"type": "text", "text": text}], "isError": error}
            )
        return self._reply(svc, msg["id"], {})

    @staticmethod
    def _reply(
        svc: FakeService, rpc_id: Any, result: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Response:
        body = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        if svc.sse:
            text = f"event: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
            return Response(text, media_type="text/event-stream", headers=headers)
        return JSONResponse(body, headers=headers)


@contextmanager
def serve_in_thread(app: Any) -> Iterator[str]:
    """在后台线程里起一个 ASGI 应用，交出 http://127.0.0.1:<端口>。"""
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake higress did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
