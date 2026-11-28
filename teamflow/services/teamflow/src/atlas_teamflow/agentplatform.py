"""Agent 平台（Atlas）接口的 HTTP 客户端。团队、流程空间、节点协作各自包一层，业务代码只依赖那几层。

用到的接口：
    智能体    GET /v1/agents、GET /v1/agents/{id}、GET|POST /v1/agents/{id}/grants
    应用工作空间（流程空间）
              POST /v1/app-workspaces（按编号幂等）、DELETE /v1/app-workspaces/{编号}
              GET  …/{编号}/files?dir=、GET …/files/download?path=、PUT …/files?path=、
              POST …/files/stat
    会话      POST /v1/threads（app_workspace = 流程空间）、POST /v1/threads/{id}/runs、
              POST /v1/runs/{id}/cancel

★ 内网免令牌：X-Uid（谁在操作）、X-Tenant-Id、X-Caller-App: teamflow，见 outbound.py。
  Agent 平台据 X-Caller-App 记下会话由 teamflow 建，之后只接受 teamflow 来开新 run。
★ 测试里用内存实现替换（tests/fakes.py），方法签名一致。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx

from .errors import Conflict, Forbidden, Invalid, NotFound, TFError, Upstream
from .outbound import outbound_headers

__all__ = ["AgentPlatform", "RunBusy"]


class RunBusy(Conflict):
    """会话里还有没结束的 run（Agent 平台的会话串行）。"""

    def __init__(self) -> None:
        super().__init__("AGENT_BUSY", "数字员工正在运行，请等它结束或先停止")


def _error(r: httpx.Response) -> TFError:
    try:
        body = r.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    message = str((err or {}).get("message") or f"Agent 平台返回 {r.status_code}")
    if r.status_code == 404:
        return NotFound("ATLAS_NOT_FOUND", message)
    if r.status_code == 403:
        return Forbidden("ATLAS_FORBIDDEN", message)
    if r.status_code in (400, 422):
        return Invalid("ATLAS_INVALID", message)
    if r.status_code == 409:
        return Conflict("ATLAS_CONFLICT", message)
    return Upstream("ATLAS_ERROR", f"Agent 平台返回 {r.status_code}：{message[:200]}")


class AgentPlatform:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=15.0, transport=transport, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {**outbound_headers(), **(kw.pop("headers", None) or {})}
        try:
            return await self._http.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Agent 平台暂不可用，请稍后重试") from exc

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._send(method, path, **kw)
        if r.status_code >= 400:
            raise _error(r)
        return r.json() if r.content else None

    # ───────────────────────────── 智能体

    async def list_agents(self, q: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 200, "status": "enabled"}
        if q:
            params["q"] = q
        return list((await self._call("GET", "/v1/agents", params=params))["data"])

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        try:
            return dict(await self._call("GET", f"/v1/agents/{agent_id}"))
        except NotFound:
            return None

    async def agent_grants(self, agent_id: str) -> dict[str, Any]:
        """{data: [{id, subject: {type, id, name}, level}], my_level}"""
        return dict(await self._call("GET", f"/v1/agents/{agent_id}/grants"))

    async def grant_use(self, agent_id: str, uids: list[UUID]) -> None:
        """以当前出站身份（要是这个智能体的 Owner）给这些人授「使用」（READ）。"""
        await self._call(
            "POST",
            f"/v1/agents/{agent_id}/grants",
            json={
                "level": "READ",
                "subjects": [{"type": "user", "id": str(u)} for u in uids],
                "include_sub": False,
            },
        )

    # ───────────────────────────── 应用工作空间

    async def open_workspace(self, space_uuid: UUID, title: str) -> dict[str, Any]:
        return dict(
            await self._call(
                "POST",
                "/v1/app-workspaces",
                json={"space_uuid": str(space_uuid), "title": title[:100]},
            )
        )

    async def release_workspace(self, space_uuid: UUID) -> None:
        r = await self._send("DELETE", f"/v1/app-workspaces/{space_uuid}")
        if r.status_code >= 400 and r.status_code != 404:
            raise _error(r)

    async def list_files(self, space_uuid: UUID, dir: str) -> dict[str, Any]:
        """{dir, dirs: [{name, path}], files: [{name, path, size, modified_at}], truncated}"""
        return dict(
            await self._call("GET", f"/v1/app-workspaces/{space_uuid}/files", params={"dir": dir})
        )

    async def stat_files(self, space_uuid: UUID, paths: list[str]) -> list[str]:
        """→ 不存在的路径。"""
        r = await self._call(
            "POST", f"/v1/app-workspaces/{space_uuid}/files/stat", json={"paths": paths}
        )
        return list(r["missing"])

    async def put_file(self, space_uuid: UUID, path: str, data: bytes) -> dict[str, Any]:
        return dict(
            await self._call(
                "PUT",
                f"/v1/app-workspaces/{space_uuid}/files",
                params={"path": path},
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
        )

    async def download(self, space_uuid: UUID, path: str) -> tuple[int, AsyncIterator[bytes]]:
        """→ (大小, 字节流)。流用完自动关连接。"""
        req = self._http.build_request(
            "GET",
            f"/v1/app-workspaces/{space_uuid}/files/download",
            params={"path": path},
            headers=outbound_headers(),
        )
        try:
            r = await self._http.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Agent 平台暂不可用，请稍后重试") from exc
        if r.status_code >= 400:
            await r.aread()
            await r.aclose()
            raise _error(r)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()

        return int(r.headers.get("content-length") or 0), body()

    # ───────────────────────────── 会话

    async def create_thread(self, agent_id: str, *, title: str, app_workspace: UUID) -> str:
        r = await self._call(
            "POST",
            "/v1/threads",
            json={"agent_id": agent_id, "title": title[:256], "app_workspace": str(app_workspace)},
        )
        return str(r["id"])

    async def create_run(
        self, thread_id: str, content: list[dict[str, Any]], *, idempotency_key: str
    ) -> str:
        """开一次 run。会话里还有没结束的 run → RunBusy。带幂等键：重发拿到同一个 run。"""
        r = await self._send(
            "POST",
            f"/v1/threads/{thread_id}/runs",
            json={"content": content},
            headers={"Idempotency-Key": idempotency_key},
        )
        if r.status_code == 409:
            raise RunBusy()
        if r.status_code >= 400:
            raise _error(r)
        return str(r.json()["run_id"])

    async def cancel_run(self, run_id: str) -> None:
        """取消；run 已经结束、也没有还在跑的子孙时 Agent 平台回 409，当作取消成功。"""
        r = await self._send("POST", f"/v1/runs/{run_id}/cancel")
        if r.status_code >= 400 and r.status_code not in (404, 409):
            raise _error(r)
