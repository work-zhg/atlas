"""Atlas 智能体的适配器：只用 atlas-server 的公开 API（doc/agent-eval-design.html §15）。

    建智能体  POST /v1/agents（每个 variant 一个，slug = bench-<variant>-<配置指纹>，复用）
    新会话    POST /v1/threads（每个 trial 一个 → 全新工作区 / 沙箱）
    初始文件  PUT  /v1/threads/{id}/files?path=…
    发一轮    POST /v1/threads/{id}/runs（带 Idempotency-Key）
    审批      GET  /v1/threads/{id}/events（SSE）里的 approval.required
              → POST /v1/runs/{rid}/approvals/{aid}
              （子智能体的审批挂在子 run 上，所以按会话流而不是按主 run 收）
    提问      approval_kind = question 的同一个事件：不按审批策略，由模拟用户作答（带 answers）；
              任务没有模拟用户时不带答案批准 = 按推荐项（doc/ask-user-question-design.html）
    等终态    GET  /v1/runs/{id} 轮询；超预算 → POST /v1/runs/{id}/cancel
    取轨迹    GET  /v1/runs/{id}/trace（含子 run）+ 各 run 的用量
    取产物    GET  /v1/threads/{id}/files + download
    清理      DELETE /v1/threads/{id}；cleanup 命令删除 bench- 开头的智能体

身份：内网免令牌（内网身份传递 §12）—— 请求头 X-Uid = 评测用户 atlas-bench（需 agent:create +
chat:use），X-Caller-App: bench。不写 X-Auth-Uid：按谁判由 Atlas 按 Agent 定。
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
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..schema import SimulatedUser, Task, VariantConfig

__all__ = ["AGENT_PREFIX", "AtlasAgentPlatform", "TrialRun"]

logger = logging.getLogger(__name__)

AGENT_PREFIX = "bench-"
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_POLL_S = 2.0
#: 下载产物的单文件上限（校验用不到大文件；超过的跳过并记下）
_DOWNLOAD_MAX = 5 * 1024 * 1024


class AtlasError(RuntimeError):
    pass


class Answerer(Protocol):
    """模拟用户（agent/user.py）：按用户心里的需求回答 Agent 的问题。"""

    async def answer(self, brief: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


@dataclass
class TrialRun:
    """一次 trial 在 Atlas 上的现场：会话、主 run、审批记录、终态。"""

    thread_id: str
    run_id: str = ""
    status: str = ""
    error: str | None = None
    #: run 失败时平台给的 error_kind（runner 据此区分平台错误与 Agent 失败）
    error_kind: str | None = None
    budget_exceeded: str | None = None
    approvals: list[dict[str, Any]] = field(default_factory=list)
    #: Agent 提的问题（ask_user_question）：每次一条 {n, headers, answers, by, error, latency_ms}。
    #: 不进 approvals —— 问题不是审批，审批策略也管不到它
    questions: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    #: 前置会话（W4）：会话 id、失败原因、开始前已有的记忆 id（None = 本题不碰记忆）
    setup_threads: list[str] = field(default_factory=list)
    setup_error: str | None = None
    memories_before: set[str] | None = None
    memories_new: int = 0
    #: 会话流上收到的事件 id（thread_seq，按到达顺序）与主动断线重连次数（E18）
    sse_ids: list[int] = field(default_factory=list)
    sse_reconnects: int = 0


def _decide(policy: str, already: int) -> str:
    """审批策略 → 这一次的决定。already = 本 trial 已经决定过几次。"""
    if policy == "deny_all" or (policy == "deny_first" and already == 0):
        return "rejected"
    return "approved"


def _fingerprint(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:8]


class AtlasAgentPlatform:
    name = "atlas"

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = dict(headers)
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
        return {"base_url": self._base, "principal": me.get("name"), "account": me.get("account")}

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
        self,
        agent: dict[str, Any],
        task: Task,
        *,
        trial: int,
        approval_policy: str,
        answerer: Answerer | None = None,
    ) -> TrialRun:
        thread = await self._req(
            "POST",
            "/v1/threads",
            json={
                "agent_id": agent["id"],
                "title": f"bench:{task.id}#{trial}",
                "memory": task.thread_memory,
            },
        )
        tr = TrialRun(thread_id=thread["id"])
        if task.thread_memory == "on":
            if thread.get("memory") != "on":
                # ★ 服务账号的会话按账号的 memory_default 开关记忆，调用方不能替它打开
                #   （services/thread.py）。这是评测环境配置问题，不是平台故障 —— 早报、别空等
                await self.delete_thread(tr)
                msg = (
                    "本题需要用户记忆，但评测服务账号的 memory_default 是 off："
                    "请在「服务账号」设置里把它打开"
                )
                raise AtlasError(msg)
            tr.memories_before = await self._memory_ids()
        if task.setup:
            await self._run_setup(agent, task, trial, tr)
            if tr.setup_error:
                tr.status, tr.error_kind, tr.error = "failed", "setup_failed", tr.setup_error
                return tr
        for path, content in task.files.items():
            await self._req(
                "PUT",
                f"/v1/threads/{tr.thread_id}/files",
                params={"path": path},
                content=content.encode("utf-8"),
            )
        listener = asyncio.create_task(
            self._approve_from_stream(
                tr, approval_policy, task.sse_disconnect_after, task.user, answerer
            )
        )
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
                    tr.error_kind = run.get("error_kind") or tr.status
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

    async def _approve_from_stream(
        self,
        tr: TrialRun,
        policy: str,
        disconnect_after: int | None = None,
        user: SimulatedUser | None = None,
        answerer: Answerer | None = None,
    ) -> None:
        """跟着会话事件流批审批（主 run 与子 run 的都在这条流上）。断了就带游标重连。

        disconnect_after：收到这么多条事件后**主动**断开、2 秒后带游标重连 ——
        检查断线续传有没有丢事件或重复（E18）。只断一次。
        """
        cursor = 0
        decided: set[str] = set()
        delay = float(policy.split(":", 1)[1]) if policy.startswith("delay:") else 0.0
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
                                tr.sse_ids.append(cursor)
                            if (
                                disconnect_after
                                and not tr.sse_reconnects
                                and len(tr.sse_ids) >= disconnect_after
                            ):
                                tr.sse_reconnects += 1
                                break
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
                        if data.get("approval_kind") == "question":
                            await self._answer_question(tr, event, data, user, answerer)
                            continue
                        t0 = time.monotonic()
                        decision = _decide(policy, len(tr.approvals))
                        if delay:
                            # 模拟「人很久之后才批」：平台不该因为等待而结束 run
                            await asyncio.sleep(delay)
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
            await asyncio.sleep(2.0 if tr.sse_reconnects else 1.0)

    async def _answer_question(
        self,
        tr: TrialRun,
        event: dict[str, Any],
        data: dict[str, Any],
        user: SimulatedUser | None,
        answerer: Answerer | None,
    ) -> None:
        """回答 Agent 的问题。模拟用户出错或答案被拒（400）时退回按推荐 —— 不能让 trial 卡住。"""
        questions = (data.get("args") or {}).get("questions") or []
        url = f"/v1/runs/{event['run_id']}/approvals/{data['approval_id']}"
        t0 = time.monotonic()
        record: dict[str, Any] = {
            "n": len(questions),
            "headers": [q.get("header") for q in questions],
            "answers": None,
            "by": "recommended",
            "error": None,
        }
        if user is not None and answerer is not None:
            try:
                answers = await answerer.answer(user.brief, questions)
                await self._req("POST", url, json={"decision": "approved", "answers": answers})
                record.update(answers=answers, by="simulated")
            except Exception as exc:  # 记下来，退回按推荐
                record["error"] = str(exc)[:300]
        if record["by"] == "recommended":
            await self._req("POST", url, json={"decision": "approved"})
        record["latency_ms"] = int((time.monotonic() - t0) * 1000)
        tr.questions.append(record)

    # ------------------------------------------------------------------ 前置会话与记忆（W4）

    async def _run_setup(self, agent: dict[str, Any], task: Task, trial: int, tr: TrialRun) -> None:
        """在另一个会话里按顺序发 setup 消息（各等到终态）；需要时再等记忆抽取出来。"""
        thread = await self._req(
            "POST",
            "/v1/threads",
            json={
                "agent_id": agent["id"],
                "title": f"bench:{task.id}#{trial}:setup",
                "memory": task.thread_memory,
            },
        )
        tr.setup_threads.append(thread["id"])
        deadline = time.monotonic() + task.budget.wall_s
        for text in task.setup:
            accepted = await self._req(
                "POST",
                f"/v1/threads/{thread['id']}/runs",
                json={"content": [{"type": "text", "text": text}]},
                headers={"Idempotency-Key": f"bench-{uuid.uuid4().hex}"},
            )
            while True:
                run = await self._req("GET", f"/v1/runs/{accepted['run_id']}")
                if run["status"] in _TERMINAL:
                    break
                if time.monotonic() > deadline:
                    tr.setup_error = "前置会话超过时间预算"
                    return
                await asyncio.sleep(_POLL_S)
            if run["status"] != "succeeded":
                tr.setup_error = (
                    f"前置会话失败：{run.get('error_kind')} {run.get('error_message') or ''}"
                )
                return
        if task.wait_memory_s and tr.memories_before is not None:
            # 记忆由 Agent 在对话中保存（要勾「记忆」工具），没勾的要等会话空闲后的补记：
            # 轮询到出现新记忆为止
            until = time.monotonic() + task.wait_memory_s
            while time.monotonic() < until:
                tr.memories_new = len(await self._memory_ids() - tr.memories_before)
                if tr.memories_new:
                    return
                await asyncio.sleep(3.0)
            tr.setup_error = f"前置会话结束后 {task.wait_memory_s:.0f}s 内没有抽取出记忆"

    async def _memory_ids(self) -> set[str]:
        """当前有效的记忆条目编号（doc/memory-design.html §7 的管理接口）。"""
        page = await self._req("GET", "/v1/memories/entries", params={"limit": 200})
        return {str(m["number"]) for m in page.get("data") or []}

    async def cleanup_memories(self, tr: TrialRun) -> int:
        """删掉本 trial 期间新增的记忆（服务账号的记忆在所有评测之间共享，留着会污染别的题）。"""
        if tr.memories_before is None:
            return 0
        removed = 0
        with contextlib.suppress(AtlasError):
            for number in await self._memory_ids() - tr.memories_before:
                # 彻底删除（连同取代链）：评测账号的记忆在各题之间共享，不能留下可恢复的副本
                await self._req("DELETE", f"/v1/memories/entries/{quote(number)}")
                removed += 1
        return removed

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
        for tid in [tr.thread_id, *tr.setup_threads]:
            with contextlib.suppress(AtlasError):
                await self._req("DELETE", f"/v1/threads/{tid}")

    async def cleanup_all(self) -> list[str]:
        """删除评测建的智能体（slug 前缀 bench-）。"""
        removed = []
        page = await self._req("GET", "/v1/agents", params={"q": AGENT_PREFIX, "limit": 200})
        for a in page["data"]:
            if a["slug"].startswith(AGENT_PREFIX):
                await self._req("DELETE", f"/v1/agents/{quote(a['id'])}")
                removed.append(a["slug"])
        return removed
