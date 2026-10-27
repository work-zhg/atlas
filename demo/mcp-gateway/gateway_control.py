"""MCP 网关 · 控制面：服务登记、元数据、复核、API Key、授权；给 Atlas 的内部接口。

    register_service  登记服务：① 接入检查（不带网关凭据访问上游，必须被拒）
                      → ② 在用户中心登记数据对象（登记人 = 所有者）
                      → ③ 带网关凭据从上游拉工具定义，机械检查、算指纹，存库
    approve           复核：批准某个工具的某一版定义（按指纹）
    issue_key         发 API Key：一个主体一把，只表示"我是谁"，不带权限
    grant             给主体授某个服务的"使用"：写的是用户中心的数据权限，网关只转交
    internal_routes   给 Atlas：元数据（含复核结论）、主体能用哪些服务、主体的 Key

本例里管理动作由 main.py 直接调方法，代替 MCP 管理员在管理端页面上操作。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from gateway_store import GatewayStore, Service, tool_meta
from hops import hop
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

APP = "mcp-gateway"  # 在用户中心里的应用
DATA_CODE = "McpServer"  # 数据编码：只表达语义，不带应用名
PERMISSION_TTL_S = 30


class ServicePermissions:
    """主体 → 能用的服务：问用户中心，结果短时缓存（不能每个 MCP 请求都查用户中心）。

    控制面（替 Atlas 回答）和数据面（每个请求判权）共用这一份：两道闸门的判断依据一致。
    """

    def __init__(self, uc_url: str) -> None:
        self.uc_url = uc_url
        self._cache: dict[str, tuple[float, set[str]]] = {}

    async def of(self, subject: str) -> set[str]:
        hit = self._cache.get(subject)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"{self.uc_url}/accessible",
                params={"app": APP, "code": DATA_CODE, "subject": subject},
            )
        found = set(r.json()["object_ids"])
        self._cache[subject] = (time.monotonic() + PERMISSION_TTL_S, found)
        return found


class GatewayControl:
    def __init__(
        self, store: GatewayStore, perms: ServicePermissions, *, higress_url: str, credential: str
    ) -> None:
        self.store, self.perms = store, perms
        self.higress_url, self.credential = higress_url, credential

    def upstream_url(self, service: Service) -> str:
        return f"{self.higress_url}/mcp-servers/{service.upstream}/"

    # ------------------------------------------------------------ 管理动作

    async def register_service(
        self, *, name: str, title: str, upstream: str, review_required: bool, operator: str
    ) -> bool:
        service = Service(name, title, upstream, review_required)
        url = self.upstream_url(service)
        hop(
            "MCP 网关",
            f"登记服务 {name}（上游 Higress:{upstream}，"
            f"{'要求' if review_required else '不要求'}复核）",
        )
        if not await self._rejects_without_credential(url):
            hop(
                "MCP 网关",
                f"接入检查不通过：不带网关凭据也能调通 {upstream}"
                " → 不允许登记（Higress 上漏开了认证）",
            )
            return False
        hop("MCP 网关", "接入检查通过：不带网关凭据被拒")
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{self.perms.uc_url}/data-objects",
                json={"app": APP, "code": DATA_CODE, "object_id": name, "owner": operator},
            )
        if r.status_code != 200:  # 用户中心登记失败则注册失败，不留"有服务、没授权数据"的状态
            return False
        self.store.services[name] = service
        await self.refresh(name)
        return True

    async def refresh(self, name: str) -> None:
        """带网关凭据从上游拉工具定义，存库。"""
        service = self.store.services[name]
        transport = StreamableHttpTransport(
            self.upstream_url(service), headers={"Authorization": f"Bearer {self.credential}"}
        )
        async with Client(transport, mode="legacy") as client:
            listed = await client.list_tools()
        metas = [tool_meta(t.model_dump(by_alias=True, exclude_none=True)) for t in listed]
        self.store.tools[name] = {m.name: m for m in metas}
        shown = "、".join(f"{m.name}={self.store.status(name, m)}" for m in metas)
        hop("MCP 网关", f"带网关凭据拉 {name} 的工具定义存库：{shown}")

    def approve(self, service: str, tool: str, *, operator: str) -> None:
        meta = self.store.tools[service][tool]
        self.store.reviews[(service, tool, meta.digest)] = "approved"
        hop("MCP 网关", f"{operator} 复核批准 {service}.{tool}（指纹 {meta.digest[:8]}）")

    def issue_key(self, subject: str) -> str:
        key = f"mk-{secrets.token_hex(6)}"
        self.store.keys[key] = subject
        hop("MCP 网关", f"给 {subject} 发 API Key {key[:7]}…")
        return key

    async def grant(self, *, operator: str, service: str, subject: str) -> None:
        async with httpx.AsyncClient() as http:
            await http.post(
                f"{self.perms.uc_url}/grants",
                json={
                    "operator": operator,
                    "code": DATA_CODE,
                    "object_id": service,
                    "subject": subject,
                    "level": "use",
                },
            )

    # ------------------------------------------------------------ 接入检查

    async def _rejects_without_credential(self, url: str) -> bool:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "access-check", "version": "1"},
            },
        }
        async with httpx.AsyncClient() as http:
            r = await http.post(
                url, json=init, headers={"Accept": "application/json, text/event-stream"}
            )
        return r.status_code in (401, 403)

    # ------------------------------------------------------------ 给 Atlas 的内部接口

    def internal_routes(self) -> list[Route]:
        async def metadata(_: Request) -> JSONResponse:
            out: list[dict[str, Any]] = []
            for name, svc in self.store.services.items():
                tools = [
                    {
                        "name": m.name,
                        "description": m.description,
                        "input_schema": m.input_schema,
                        "read_only": m.read_only,
                        "digest": m.digest,
                        "status": self.store.status(name, m),
                    }
                    for m in self.store.tools.get(name, {}).values()
                ]
                out.append(
                    {"name": name, "title": svc.title, "enabled": svc.enabled, "tools": tools}
                )
            hop("MCP 网关", f"内部接口：给 Atlas 元数据（{len(out)} 个服务，含复核结论）")
            return JSONResponse({"services": out})

        async def services_of(request: Request) -> JSONResponse:
            subject = request.path_params["subject"]
            usable = await self.perms.of(subject)
            found = sorted(
                n for n in usable if n in self.store.services and self.store.services[n].enabled
            )
            hop("MCP 网关", f"内部接口：{subject} 能用的服务 {found}")
            return JSONResponse({"services": found})

        async def key_of(request: Request) -> JSONResponse:
            subject = request.path_params["subject"]
            key = self.store.key_of(subject)
            hop("MCP 网关", f"内部接口：{subject} 的 Key → {key[:7] + '…' if key else '没有'}")
            return JSONResponse({"key": key}, status_code=200 if key else 404)

        return [
            Route("/internal/metadata", metadata),
            Route("/internal/subjects/{subject}/services", services_of),
            Route("/internal/subjects/{subject}/key", key_of),
        ]
