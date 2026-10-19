"""Atlas 客户端：只用它的 Agent（TeamFlow 不创建、不配置 Agent，团队设计 §07）。

★ 内网免令牌（内网身份传递 §10）：每个请求带 X-Uid（谁在操作 / 流程发起人）、X-Tenant-Id、
  X-Caller-App: teamflow，不写 X-Auth-Uid（按谁判由 Atlas 按 Agent 定）。见 outbound.py。
  Atlas 据 X-Caller-App 把会话记为 TeamFlow 的（source_app），之后 TeamFlow 来访时信任它
  已做过授权（执行角色校验），团队其他执行人也能经代理看到、操作同一个会话。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import Upstream
from .outbound import outbound_headers
from .settings import TFSettings

__all__ = ["AtlasClient", "AtlasEvent"]


@dataclass(frozen=True)
class AtlasEvent:
    """Atlas 会话事件流的一帧（TraceEvent 的子集）。"""

    type: str
    run_id: str
    thread_seq: int
    data: dict[str, Any]


class AtlasClient:
    def __init__(
        self, settings: TFSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.atlas_base_url, timeout=10.0, transport=transport, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_agents(
        self, *, q: str | None = None, status: str | None = "enabled"
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 200}
        if q:
            params["q"] = q
        if status:
            params["status"] = status
        try:
            r = await self._http.get("/v1/agents", params=params, headers=outbound_headers())
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Atlas 暂不可用，请稍后重试") from exc
        return list(r.json()["data"])

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """→ Agent 详情；不存在返回 None。"""
        try:
            r = await self._http.get(f"/v1/agents/{agent_id}", headers=outbound_headers())
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Atlas 暂不可用，请稍后重试") from exc
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise Upstream("ATLAS_ERROR", f"Atlas 返回 {r.status_code}")
        return dict(r.json())

    # ───────────────────────────── 会话与运行（Agent 适配层专用）

    async def create_thread(
        self, agent_id: str, title: str, *, git_profile: str | None = None
    ) -> str:
        """建会话。git_profile：Atlas 的 git 凭据档案名，会话 Pod 里的 git CLI 据此获得凭据。"""
        body: dict[str, Any] = {"agent_id": agent_id, "title": title[:256]}
        if git_profile:
            body["git_profile"] = git_profile
        r = await self._post("/v1/threads", body)
        return str(r["id"])

    async def create_run(self, thread_id: str, text: str, idempotency_key: str) -> str:
        """发一条消息并创建 run。★ 带幂等键：重启后重发同一条指令不会变成两条消息。"""
        r = await self._post(
            f"/v1/threads/{thread_id}/runs",
            {"content": [{"type": "text", "text": text}]},
            headers={"Idempotency-Key": idempotency_key},
        )
        return str(r["run_id"])

    async def stream_thread(self, thread_id: str, after_seq: int) -> AsyncIterator[AtlasEvent]:
        """订阅会话事件流（永不自动结束，由调用方在 run 终态时退出）。

        ★ 不设读超时：Agent 运行可能持续数十分钟，Atlas 会定时发心跳。
        """
        async with self._http.stream(
            "GET",
            f"/v1/threads/{thread_id}/events",
            params={"after_seq": after_seq},
            headers=outbound_headers(),
            timeout=httpx.Timeout(10.0, read=None),
        ) as r:
            if r.status_code >= 400:
                raise Upstream("ATLAS_ERROR", f"Atlas 事件流返回 {r.status_code}")
            data: list[str] = []
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line == "" and data:
                    ev = json.loads("\n".join(data))
                    data = []
                    yield AtlasEvent(
                        type=ev["type"],
                        run_id=str(ev["run_id"]),
                        thread_seq=int(ev.get("thread_seq") or 0),
                        data=ev.get("data") or {},
                    )

    async def _post(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        try:
            r = await self._http.post(
                path, json=body, headers={**outbound_headers(), **(headers or {})}
            )
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Atlas 暂不可用，请稍后重试") from exc
        if r.status_code == 409:
            # 同一会话已有运行中的 run（如人刚经代理发了一条）：不是故障，调用方稍后重试
            raise Upstream("ATLAS_BUSY", "Agent 正在运行")
        if r.status_code >= 400:
            raise Upstream("ATLAS_ERROR", f"Atlas 返回 {r.status_code}：{r.text[:200]}")
        return r.json()

    # ───────────────────────────── 透传（人机协同代理用，见 api/atlas_proxy.py）

    async def raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """原样转发一个请求，返回 Atlas 的响应（状态码与响应体不做解释）。"""
        try:
            return await self._http.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={**outbound_headers(), **(headers or {})},
            )
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Atlas 暂不可用，请稍后重试") from exc

    async def raw_stream(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, AsyncIterator[bytes]]:
        """原样转发一个流式 GET（会话事件 SSE）→ (状态码, 字节流)。不设读超时（Atlas 有心跳）。"""
        req = self._http.build_request(
            "GET",
            path,
            params=params,
            headers={**outbound_headers(), **(headers or {})},
            timeout=httpx.Timeout(10.0, read=None),
        )
        try:
            r = await self._http.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise Upstream("ATLAS_UNAVAILABLE", "Atlas 暂不可用，请稍后重试") from exc

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()

        return r.status_code, body()
