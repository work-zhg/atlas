"""MCP 网关 · 数据面：每个 MCP 请求都过这一层 —— Atlas 的 Native 调用、Pod 里的 CLI 都一样。

    ① 认人      Authorization 里的 API Key → 主体（查网关自己的库）；没有 / 无效 → 401
    ② 判权      主体对这个服务有"使用"？（问用户中心，短缓存）没有 → 403
    ③ 复核      tools/call：工具没通过复核 → 403
                tools/list：返回里去掉没通过复核的工具 —— 按返回里这版定义的指纹判，
                上游悄悄改过定义、指纹对不上，同样去掉
    ④ 改写身份  去掉调用方自带的 X-Auth-Uid，写入 Key 认出的主体；X-Uid（使用者）照转，只作审计
    ⑤ 转发      主体 Key 换成网关凭据，转给 Higress 上对应的服务（上游从注册表查）
    ⑥ 访问日志

拒绝时回 JSON-RPC 错误体：mcp 2.x 客户端能把原因原样交给调用方（只有状态码的话它只说"出错了"）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from gateway_control import GatewayControl
from gateway_store import digest_of
from hops import hop
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

#: 不往上游转的请求头：主体 Key、调用方自带的权限主体，以及逐跳的头
DROPPED = {"authorization", "x-auth-uid", "host", "content-length", "connection"}


def _rpc_error(status: int, rpc_id: Any, message: str) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": message}}
    return JSONResponse(body, status_code=status)


class GatewayProxy:
    def __init__(self, control: GatewayControl) -> None:
        self.control = control
        self.store = control.store

    def route(self) -> Route:
        return Route("/mcp/{service}", self.handle, methods=["GET", "POST", "DELETE"])

    async def handle(self, request: Request) -> Response:
        name = request.path_params["service"]
        body = await request.body()
        msg = json.loads(body) if body else {}
        method, rpc_id = msg.get("method", request.method), msg.get("id")
        what = f"{method} {msg['params']['name']}" if method == "tools/call" else method

        service = self.store.services.get(name)
        if service is None or not service.enabled:
            return self._deny(404, rpc_id, name, what, None, f"MCP 服务 {name} 不存在或已停用")
        # ① 认人
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        subject = self.store.keys.get(key)
        if subject is None:
            return self._deny(401, rpc_id, name, what, None, "API Key 无效或已停用")
        # ② 判权
        if name not in await self.control.perms.of(subject):
            return self._deny(
                403, rpc_id, name, what, subject, f"{subject} 没有被授权使用 MCP 服务 {name}"
            )
        # ③ 复核（调用）
        if method == "tools/call" and not self.store.allows(name, msg["params"]["name"]):
            return self._deny(
                403, rpc_id, name, what, subject, f"工具 {msg['params']['name']} 没有通过复核"
            )

        if rpc_id is not None:  # 只打印请求；通知（notifications/*）、推送流的 GET 不打印，免得刷屏
            forged = request.headers.get("x-auth-uid")
            note = f"（丢掉调用方自带的 {forged}）" if forged and forged != subject else ""
            checked = " · 复核 ✓" if method == "tools/call" else ""
            hop(
                "MCP 网关",
                f"{what:<24} {name:<7} ← Key→{subject} · 判权 ✓{checked}"
                f" → X-Auth-Uid={subject}{note}、换网关凭据 → Higress",
            )

        # ④ 改写身份 ⑤ 转发
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROPPED}
        headers |= {"Authorization": f"Bearer {self.control.credential}", "X-Auth-Uid": subject}
        async with httpx.AsyncClient(timeout=60) as http:
            upstream = await http.request(
                request.method, self.control.upstream_url(service), content=body, headers=headers
            )
        content = upstream.content
        # ③ 复核（列表）
        if method == "tools/list" and upstream.status_code == 200:
            reply = upstream.json()
            tools = reply["result"]["tools"]
            kept = [t for t in tools if self.store.allows(name, t["name"], digest_of(t))]
            if dropped := [t["name"] for t in tools if t not in kept]:
                hop("MCP 网关", f"tools/list 的返回里去掉未通过复核的 {dropped}")
            reply["result"]["tools"] = kept
            content = json.dumps(reply, ensure_ascii=False).encode()
        self._log(name, what, subject, "ok", upstream.status_code)
        passthrough = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() in ("content-type", "mcp-session-id")
        }
        return Response(content, status_code=upstream.status_code, headers=passthrough)

    def _deny(
        self, status: int, rpc_id: Any, service: str, what: str, subject: str | None, reason: str
    ) -> JSONResponse:
        hop("MCP 网关", f"{what:<24} {service:<7} ← 拒绝（HTTP {status}）：{reason}")
        self._log(service, what, subject, reason, status)
        return _rpc_error(status, rpc_id, reason)

    def _log(self, service: str, what: str, subject: str | None, result: str, status: int) -> None:
        self.store.access_log.append(
            {
                "service": service,
                "request": what,
                "subject": subject,
                "result": result,
                "status": status,
            }
        )
