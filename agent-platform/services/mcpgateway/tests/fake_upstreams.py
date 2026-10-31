"""测试用的上游：网关直连的三种服务，都在后台线程里跑，网关经真实的网络调它们。

    FakeMcp    /mcp/{名字}：最小的 MCP 服务（Streamable HTTP，JSON 响应）。手写而不是用 SDK：
               测试要在运行中改工具定义、让它回 JSON-RPC 错误、记下每个请求的请求头
    FakeRest   OpenAPI 文档 + 几个接口（客户管理），记下每个请求
    FakeGrpc   gRPC 服务（程序里拼出的 proto 描述）+ 服务端反射，记下每个调用的 metadata

★ 都要求凭据（MCP：Bearer；REST：X-Api-Key；gRPC：authorization metadata）：
  网关注入的上游凭据有没有带上，一看就知道。
"""

from __future__ import annotations

import inspect
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

MCP_TOKEN = "mcp-upstream-token"
REST_KEY = "rest-upstream-key"
GRPC_TOKEN = "Bearer grpc-upstream-token"


# ───────────────────────────── MCP


class ToolFailure(Exception):
    """工具执行失败：作为 isError 的结果返回。"""


class RpcFailure(Exception):
    """上游回 JSON-RPC 错误。"""


@dataclass
class FakeMcpService:
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    handlers: dict[str, Callable[[dict[str, Any], dict[str, str]], Any]] = field(
        default_factory=dict
    )
    #: 每个请求：(JSON-RPC 方法, 请求头, params)
    seen: list[tuple[str, dict[str, str], dict[str, Any]]] = field(default_factory=list)

    def tool(
        self,
        name: str,
        description: str,
        handler: Callable[[dict[str, Any], dict[str, str]], Any],
        *,
        properties: dict[str, Any] | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> None:
        self.tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}},
            **({"annotations": annotations} if annotations else {}),
        }
        self.handlers[name] = handler

    def calls(self) -> list[tuple[dict[str, str], dict[str, Any]]]:
        return [(h, p) for m, h, p in self.seen if m == "tools/call"]


@dataclass
class FakeMcp:
    services: dict[str, FakeMcpService] = field(default_factory=dict)

    def routes(self) -> list[Route]:
        return [Route("/mcp/{name}", self.handle, methods=["GET", "POST", "DELETE"])]

    async def handle(self, request: Request) -> Response:
        svc = self.services.get(request.path_params["name"])
        if svc is None:
            return Response(status_code=404)
        if request.headers.get("authorization") != f"Bearer {MCP_TOKEN}":
            return Response(status_code=401)
        if request.method != "POST":
            return Response(status_code=405)
        msg = json.loads(await request.body())
        method, params = msg.get("method"), msg.get("params") or {}
        svc.seen.append((method, dict(request.headers), params))
        if "id" not in msg:
            return Response(status_code=202)
        rid = msg["id"]
        if method == "initialize":
            result: Any = {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": list(svc.tools.values())}
        elif method == "tools/call":
            handler = svc.handlers.get(params.get("name"))
            if handler is None:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "no tool"}}
                )
            try:
                text = handler(params.get("arguments") or {}, dict(request.headers))
                if inspect.isawaitable(text):
                    text = await text
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except ToolFailure as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except RpcFailure as exc:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}
                )
        elif method == "ping":
            result = {}
        else:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}}
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid, "result": result},
            headers={"mcp-session-id": "upstream-session"},
        )


# ───────────────────────────── REST


OPENAPI: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "CRM", "version": "1"},
    "paths": {
        "/customers/{id}": {
            "get": {
                "operationId": "getCustomer",
                "summary": "按客户编号读取客户资料",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"content": {"application/json": {}}}},
            },
            "delete": {
                "operationId": "deleteCustomer",
                "summary": "删除客户",
                "parameters": [{"$ref": "#/components/parameters/Id"}],
                "responses": {"204": {}},
            },
        },
        "/customers": {
            "get": {
                "operationId": "searchCustomers",
                "summary": "搜索客户",
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"content": {"application/json": {}}}},
            }
        },
        "/tickets": {
            "post": {
                "operationId": "createTicket",
                "summary": "新建工单",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Ticket"}}
                    }
                },
                "responses": {"201": {"content": {"application/json": {}}}},
            }
        },
        "/attachments": {
            "post": {
                "operationId": "uploadAttachment",
                "requestBody": {"content": {"multipart/form-data": {}}},
                "responses": {"200": {}},
            }
        },
        "/fail": {"get": {"operationId": "alwaysFail", "responses": {"200": {}}}},
    },
    "components": {
        "parameters": {
            "Id": {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        },
        "schemas": {
            "Ticket": {
                "type": "object",
                "required": ["customer_id", "title"],
                "properties": {
                    "customer_id": {"type": "string", "description": "客户编号"},
                    "title": {"type": "string"},
                    "priority": {"type": "string"},
                },
            }
        },
    },
}


@dataclass
class FakeRest:
    #: 每个请求：(方法, 路径, 查询参数, 请求头, 请求体)
    seen: list[tuple[str, str, dict[str, str], dict[str, str], Any]] = field(default_factory=list)

    def routes(self) -> list[Route]:
        return [
            Route("/rest/openapi.json", self.spec, methods=["GET"]),
            Route("/rest/api/{path:path}", self.api, methods=["GET", "POST", "PATCH", "DELETE"]),
        ]

    async def spec(self, request: Request) -> Response:
        return JSONResponse(OPENAPI)

    async def api(self, request: Request) -> Response:
        body = await request.body()
        parsed = json.loads(body) if body else None
        path = "/" + request.path_params["path"]
        self.seen.append(
            (request.method, path, dict(request.query_params), dict(request.headers), parsed)
        )
        if request.headers.get("x-api-key") != REST_KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if path == "/fail":
            return JSONResponse({"error": "boom"}, status_code=500)
        if path.startswith("/customers/") and request.method == "GET":
            cid = path.rsplit("/", 1)[1]
            return JSONResponse({"id": cid, "name": "云帆科技", "level": "B"})
        if path == "/customers":
            q = request.query_params.get("q")
            return JSONResponse({"total": 1, "items": [{"id": "C1", "name": q}]})
        if path == "/tickets":
            return JSONResponse({"ticket_id": "T-1", **(parsed or {})}, status_code=201)
        return JSONResponse({"error": "not found"}, status_code=404)


# ───────────────────────────── gRPC


def order_descriptor() -> Any:
    """demo.v1.OrderService 的描述（相当于 protoc 编出来的 FileDescriptorProto）。"""
    from google.protobuf import descriptor_pb2

    T = descriptor_pb2.FieldDescriptorProto
    f = descriptor_pb2.FileDescriptorProto(
        name="demo/order.proto", package="demo.v1", syntax="proto3"
    )

    def msg(name: str, *fields: tuple[str, int, int, int, str | None]) -> None:
        m = f.message_type.add(name=name)
        for fname, number, ftype, label, type_name in fields:
            fd = m.field.add(name=fname, number=number, type=ftype, label=label)
            if type_name:
                fd.type_name = type_name

    opt, rep = T.LABEL_OPTIONAL, T.LABEL_REPEATED
    msg("GetOrderRequest", ("order_id", 1, T.TYPE_STRING, opt, None))
    msg(
        "Order",
        ("order_id", 1, T.TYPE_STRING, opt, None),
        ("status", 2, T.TYPE_STRING, opt, None),
        ("amount", 3, T.TYPE_INT32, opt, None),
        ("tags", 4, T.TYPE_STRING, rep, None),
    )
    msg("Item", ("sku", 1, T.TYPE_STRING, opt, None), ("qty", 2, T.TYPE_INT32, opt, None))
    svc = f.service.add(name="OrderService")
    svc.method.add(
        name="GetOrder", input_type=".demo.v1.GetOrderRequest", output_type=".demo.v1.Order"
    )
    svc.method.add(
        name="ListItems",
        input_type=".demo.v1.GetOrderRequest",
        output_type=".demo.v1.Item",
        server_streaming=True,
    )
    svc.method.add(
        name="DeleteOrder", input_type=".demo.v1.GetOrderRequest", output_type=".demo.v1.Order"
    )
    svc.method.add(
        name="Chat",
        input_type=".demo.v1.Item",
        output_type=".demo.v1.Item",
        client_streaming=True,
        server_streaming=True,
    )
    return f


@dataclass
class FakeGrpc:
    address: str = ""
    #: 每个调用：(方法, metadata, 请求)
    seen: list[tuple[str, dict[str, str], dict[str, Any]]] = field(default_factory=list)
    _server: Any = None

    def descriptor_set(self) -> bytes:
        from google.protobuf import descriptor_pb2

        return descriptor_pb2.FileDescriptorSet(file=[order_descriptor()]).SerializeToString()

    def start(self) -> None:
        import grpc
        from google.protobuf import descriptor_pool, json_format, message_factory
        from grpc_reflection.v1alpha import reflection

        pool = descriptor_pool.DescriptorPool()
        pool.Add(order_descriptor())
        cls = {
            n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"demo.v1.{n}"))
            for n in ("GetOrderRequest", "Order", "Item")
        }

        def record(name: str, request: Any, context: Any) -> dict[str, str]:
            md = {k: v for k, v in context.invocation_metadata()}
            self.seen.append(
                (name, md, json_format.MessageToDict(request, preserving_proto_field_name=True))
            )
            if md.get("authorization") != GRPC_TOKEN:
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad token")
            return md

        def get_order(request: Any, context: Any) -> Any:
            record("GetOrder", request, context)
            return cls["Order"](order_id=request.order_id, status="PAID", amount=1299, tags=["vip"])

        def list_items(request: Any, context: Any) -> Iterator[Any]:
            record("ListItems", request, context)
            for i in range(3):
                yield cls["Item"](sku=f"SKU-{i}", qty=i + 1)

        def delete_order(request: Any, context: Any) -> Any:
            record("DeleteOrder", request, context)
            context.abort(grpc.StatusCode.NOT_FOUND, f"order {request.order_id} not found")

        handler = grpc.method_handlers_generic_handler(
            "demo.v1.OrderService",
            {
                "GetOrder": grpc.unary_unary_rpc_method_handler(
                    get_order,
                    request_deserializer=cls["GetOrderRequest"].FromString,
                    response_serializer=cls["Order"].SerializeToString,
                ),
                "ListItems": grpc.unary_stream_rpc_method_handler(
                    list_items,
                    request_deserializer=cls["GetOrderRequest"].FromString,
                    response_serializer=cls["Item"].SerializeToString,
                ),
                "DeleteOrder": grpc.unary_unary_rpc_method_handler(
                    delete_order,
                    request_deserializer=cls["GetOrderRequest"].FromString,
                    response_serializer=cls["Order"].SerializeToString,
                ),
            },
        )
        server = grpc.server(ThreadPoolExecutor(max_workers=4))
        server.add_generic_rpc_handlers((handler,))
        reflection.enable_server_reflection(
            ["demo.v1.OrderService", reflection.SERVICE_NAME], server, pool=pool
        )
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self._server = server
        self.address = f"127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=None)


# ───────────────────────────── 起服务


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serve_in_thread(app: Starlette) -> Iterator[str]:
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("测试上游没起来")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def http_upstreams(mcp: FakeMcp, rest: FakeRest) -> Starlette:
    return Starlette(routes=[*mcp.routes(), *rest.routes()])
