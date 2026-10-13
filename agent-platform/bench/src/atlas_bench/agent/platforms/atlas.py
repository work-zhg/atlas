"""Atlas 智能体的适配器：只用 atlas-server 的公开 API（doc/agent-eval-design.html §9.1）。

    建智能体  POST /v1/agents（每个 variant 一个，slug = bench-<variant>-<配置指纹>，复用）
    新会话    POST /v1/threads（每个 trial 一个 → 全新工作区 / 沙箱）
    初始文件  PUT  /v1/threads/{id}/files?path=…
    发一轮    POST /v1/threads/{id}/runs（带 Idempotency-Key）
    审批      GET  /v1/threads/{id}/events（SSE）里的 approval.required
              → POST /v1/runs/{rid}/approvals/{aid}
              （子智能体的审批挂在子 run 上，所以按会话流而不是按主 run 收）
    等终态    GET  /v1/runs/{id} 轮询；超预算 → POST /v1/runs/{id}/cancel
    取轨迹    GET  /v1/runs/{id}/trace（含子 run）+ 各 run 的用量
    取产物    GET  /v1/threads/{id}/files + download
    清理      DELETE /v1/threads/{id}；cleanup 命令删除 bench- 开头的智能体

身份：评测专用服务账号（Bearer atlas_sk_…，需 agent:create + chat:use）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from ..schema import Task, VariantConfig

__all__ = ["AGENT_PREFIX", "AtlasAgentPlatform", "TrialRun"]

logger = logging.getLogger(__name__)

AGENT_PREFIX = "bench-"
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_POLL_S = 2.0
#: 下载产物的单文件上限（校验用不到大文件；超过的跳过并记下）
_DOWNLOAD_MAX = 5 * 1024 * 1024


class AtlasError(RuntimeError):
    pass


@dataclass
class TrialRun:
    """一次 trial 在 Atlas 上的现场：会话、主 run、审批记录、终态。"""

    thread_id: str
    run_id: str = ""
    status: str = ""
    error: str | None = None
    budget_exceeded: str | None = None
    approvals: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


def _fingerprint(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:8]


class AtlasAgentPlatform:
    name = "atlas"

    def __init__(
        self, base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._c = httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=60.0,
            trust_env=False,
            transport=transport,
        )
        self._transport = transport
        self._agents: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self._c.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.json().get("message") or r.text[:300]
            except ValueError:
                detail = r.text[:300]
            raise AtlasError(f"{method} {path} → {r.status_code}：{detail}")
        return None if r.status_code == 204 else r.json()

    async def describe(self) -> dict[str, Any]:
        me = await self._req("GET", "/v1/me")
        return {"base_url": self._base, "principal": me.get("name"), "kind": me.get("kind")}

    # ------------------------------------------------------------------ 智能体

    async def prepare(self, variant: str, cfg: VariantConfig) -> dict[str, Any]:
        """确保 variant 对应的智能体存在（配置变了 = 指纹变了 = 另一个智能体）。"""
        slug = f"{AGENT_PREFIX}{variant}-{_fingerprint(cfg.agent)}".lower().replace("_", "-")[:64]
        if slug in self._agents:
            return self._agents[slug]
        page = await self._req("GET", "/v1/agents", params={"q": slug, "limit": 50})
        agent = next((a for a in page["data"] if a["slug"] == slug), None)
        if agent is None:
            agent = await self._req(
                "POST",
                "/v1/agents",
                json={
                    "slug": slug,
                    "name": f"bench:{variant}",
                    "description": cfg.description or "atlas-bench 自动创建的评测智能体，可删除",
                    "spec": cfg.agent,
                },
            )
            logger.info("建了评测智能体 %s", slug)
        if agent.get("status") != "enabled":
            # ★ 新建的智能体不是启用状态，而服务账号只能用已启用的（可用范围 all_enabled）
            agent = await self._req(
                "POST", f"/v1/agents/{agent['id']}/status", json={"status": "enabled"}
            )
        self._agents[slug] = agent
        return agent

    # ------------------------------------------------------------------ 一次 trial

    async def run_trial(
        self, agent: dict[str, Any], task: Task, *, trial: int, approval_policy: str
    ) -> TrialRun:
        thread = await self._req(
            "POST",
            "/v1/threads",
            json={"agent_id": agent["id"], "title": f"bench:{task.id}#{trial}", "memory": "off"},
        )
        tr = TrialRun(thread_id=thread["id"])
        for path, content in task.files.items():
            await self._req(
                "PUT",
                f"/v1/threads/{tr.thread_id}/files",
                params={"path": path},
                content=content.encode("utf-8"),
            )
        listener = asyncio.create_task(self._approve_from_stream(tr, approval_policy))
        try:
            accepted = await self._req(
                "POST",
                f"/v1/threads/{tr.thread_id}/runs",
                json={"content": [{"type": "text", "text": task.instruction}]},
                headers={"Idempotency-Key": f"bench-{uuid.uuid4().hex}"},
            )
            tr.run_id = accepted["run_id"]
            await self._wait(tr, task)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        return tr

    async def _wait(self, tr: TrialRun, task: Task) -> None:
        while True:
            run = await self._req("GET", f"/v1/runs/{tr.run_id}")
            tr.status = run["status"]
            if tr.status in _TERMINAL:
                if tr.status != "succeeded":
                    tr.error = (
                        f"{run.get('error_kind') or tr.status}：{run.get('error_message') or ''}"
                    )
                return
            elapsed = time.monotonic() - tr.started
            over = None
            if elapsed > task.budget.wall_s:
                over = f"超过时间预算 {task.budget.wall_s:.0f}s"
            elif int(run.get("total_tokens") or 0) > task.budget.max_tokens:
                over = f"超过 token 预算 {task.budget.max_tokens}"
            if over:
                tr.budget_exceeded = over
                with contextlib.suppress(AtlasError):
                    await self._req("POST", f"/v1/runs/{tr.run_id}/cancel")
                return
            await asyncio.sleep(_POLL_S)

    async def _approve_from_stream(self, tr: TrialRun, policy: str) -> None:
        """跟着会话事件流批审批（主 run 与子 run 的都在这条流上）。断了就带游标重连。"""
        cursor = 0
        decided: set[str] = set()
        while True:
            try:
                async with (
                    httpx.AsyncClient(
                        base_url=self._base,
                        headers=self._headers,
                        timeout=httpx.Timeout(30.0, read=None),
                        trust_env=False,
                        transport=self._transport,
                    ) as client,
                    client.stream(
                        "GET", f"/v1/threads/{tr.thread_id}/events", params={"after_seq": cursor}
                    ) as resp,
                ):
                    async for line in resp.aiter_lines():
                        if line.startswith("id:"):
                            with contextlib.suppress(ValueError):
                                cursor = int(line[3:].strip())
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if event.get("type") != "approval.required":
                            continue
                        data = event.get("data") or {}
                        aid = str(data.get("approval_id") or "")
                        if not aid or aid in decided:
                            continue
                        decided.add(aid)
                        decision = "approved" if policy == "approve_all" else "rejected"
                        t0 = time.monotonic()
                        await self._req(
                            "POST",
                            f"/v1/runs/{event['run_id']}/approvals/{aid}",
                            json={"decision": decision},
                        )
                        tr.approvals.append(
                            {
                                "tool": data.get("tool_name"),
                                "decision": decision,
                                "latency_ms": int((time.monotonic() - t0) * 1000),
                            }
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 断线：稍后带游标重连
                logger.debug("会话事件流断开：%s", exc)
            # 流结束（正常不会）或断开：都稍等再带游标重连，不空转
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ 收集

    async def collect(self, tr: TrialRun) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """→ (轨迹事件, 涉及的各 run 的用量)。"""
        if not tr.run_id:
            return [], []
        trace = (await self._req("GET", f"/v1/runs/{tr.run_id}/trace"))["data"]
        run_ids = list(dict.fromkeys([tr.run_id, *(str(e["run_id"]) for e in trace)]))
        runs = []
        for rid in run_ids:
            with contextlib.suppress(AtlasError):
                runs.append(await self._req("GET", f"/v1/runs/{rid}"))
        return trace, runs

    async def download_workspace(self, tr: TrialRun) -> tuple[dict[str, bytes], list[str]]:
        listing = await self._req("GET", f"/v1/threads/{tr.thread_id}/files")
        files: dict[str, bytes] = {}
        skipped: list[str] = []
        for f in listing.get("data") or []:
            if int(f.get("size") or 0) > _DOWNLOAD_MAX:
                skipped.append(f["path"])
                continue
            r = await self._c.get(
                f"/v1/threads/{tr.thread_id}/files/download", params={"path": f["path"]}
            )
            if r.status_code == 200:
                files[f["path"]] = r.content
        return files, skipped

    async def delete_thread(self, tr: TrialRun) -> None:
        with contextlib.suppress(AtlasError):
            await self._req("DELETE", f"/v1/threads/{tr.thread_id}")

    async def cleanup_all(self) -> list[str]:
        """删除评测建的智能体（slug 前缀 bench-）。"""
        removed = []
        page = await self._req("GET", "/v1/agents", params={"q": AGENT_PREFIX, "limit": 200})
        for a in page["data"]:
            if a["slug"].startswith(AGENT_PREFIX):
                await self._req("DELETE", f"/v1/agents/{quote(a['id'])}")
                removed.append(a["slug"])
        return removed
