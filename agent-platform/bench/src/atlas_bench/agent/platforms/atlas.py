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
    多轮      上一轮到终态、已触发的故障动作做完后，同一会话再发下一轮（task.turns）；
              每一轮的终态按任务声明的预期判断（task.run_outcomes），预期内的失败不记错误
    故障注入  会话流上的每条事件交给 FaultInjector（agent/faults.py），按触发条件操作集群
    事件留存  会话流上的事件全部留下（TrialRun.stream）：增量（message.delta）不归档，
              「正文 = 增量拼起来」「序号连续」这类检查只能对着它做。收尾前等流上出现各轮的终止事件
    取轨迹    GET  /v1/runs/{id}/trace（含子 run）+ 各 run 的用量
    取产物    GET  /v1/threads/{id}/files + download
    原地校验  POST /v1/threads/{id}/sandbox/exec（sandbox 模式：在 Agent 用过的评测档沙箱里执行）
    清理      DELETE /v1/threads/{id}；cleanup 命令删除 bench- 开头的智能体

身份：内网免令牌（内网身份传递 §12）—— 请求头 X-Uid = 评测用户 atlas-bench（需 agent:create +
chat:use），X-Caller-App: bench。不写 X-Auth-Uid：按谁判由 Atlas 按 Agent 定。
"""

from __future__ import annotations

import asyncio
import base64
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

from ..cluster import ClusterOps
from ..faults import FaultInjector
from ..marks import MARK_DIR, command_of
from ..schema import RunExpect, SimulatedUser, Task, VariantConfig

__all__ = ["AGENT_PREFIX", "AtlasAgentPlatform", "TrialRun"]

logger = logging.getLogger(__name__)

AGENT_PREFIX = "bench-"
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_POLL_S = 2.0
#: GET 遇到连接断开时的重试间隔；重试多久由 retry_window_s 定（默认 60 秒 ≈ 等端口转发重新拉起，
#: 见 deploy/local/forward.sh；故障集要盖过 server Pod 重建）
_GET_RETRY_S = 2.0
#: 故障动作找会话的沙箱 Pod：等计数脚本的 start 文件出现最多这么久
_MARK_WAIT_S = 30.0
#: 下载产物的单文件上限（校验用不到大文件；超过的跳过并记下）
_DOWNLOAD_MAX = 5 * 1024 * 1024
#: run 的终止事件（流上见到它 = 这个 run 的事件都到了）
_TERMINAL_EVENTS = frozenset({"run.finished", "run.failed", "run.cancelled"})
#: 「智能体开始干活」的事件：发出一轮到第一条这类事件的时间就是首轮响应时间
_WORK_EVENTS = frozenset(
    {"message.delta", "thinking.delta", "tool.started", "todos.updated", "context.usage"}
)
#: 收尾时等流上出现各轮终止事件最多这么久（轮询 GET 先看到终态，流上的事件可能还在路上）
_DRAIN_S = 20.0


class AtlasError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        #: 平台回的 HTTP 状态码；连不上时为 None
        self.status = status


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
    #: 本 trial 发出的每一轮的 run id（第一个就是 run_id）
    run_ids: list[str] = field(default_factory=list)
    #: 注入的故障动作记录（FaultInjector.records）与没触发的动作数
    actions: list[dict[str, Any]] = field(default_factory=list)
    actions_pending: int = 0
    #: 会话流上收到的全部事件（到达顺序，含不归档的增量）与流上已见到终止事件的 run
    stream: list[dict[str, Any]] = field(default_factory=list)
    stream_ended: set[str] = field(default_factory=set)
    #: 每个 run 在流上第一条干活的事件（_WORK_EVENTS）到达的时刻（monotonic）
    first_work: dict[str, float] = field(default_factory=dict)
    #: 每一轮：{n, run_id, status, error_kind, error, expected, wall_s, first_event_s}
    turns: list[dict[str, Any]] = field(default_factory=list)
    #: 评测取消 run 的时刻（monotonic）：取消到终态用了多久
    cancelled_at: dict[str, float] = field(default_factory=dict)
    #: 会话标题的来源（title_within_s 检查）
    title_source: str | None = None


def _decide(policy: str, already: int) -> str:
    """审批策略 → 这一次的决定。already = 本 trial 已经决定过几次。"""
    if policy == "deny_all" or (policy == "deny_first" and already == 0):
        return "rejected"
    return "approved"


def _keep(tr: TrialRun, event: dict[str, Any]) -> None:
    """流上的一条事件留下来，并记下终止、开始干活的时刻。"""
    tr.stream.append(event)
    run_id = str(event.get("run_id") or "")
    if event.get("type") in _TERMINAL_EVENTS:
        tr.stream_ended.add(run_id)
    elif event.get("type") in _WORK_EVENTS:
        tr.first_work.setdefault(run_id, time.monotonic())


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
        #: GET 连不上时重试多久（秒）
        self.retry_window_s = 60.0

    async def aclose(self) -> None:
        await self._c.aclose()

    async def _req(self, method: str, path: str, *, retry: bool = False, **kw: Any) -> Any:
        # ★ 连接断开（端口转发重连中）：GET 幂等，等一会儿再试；其余请求重发会出副作用（重复发
        #   消息），默认不重试 —— 调用方确认重发安全（如审批决定）时用 retry 打开。
        #   都转成 AtlasError：只记这个 trial 平台错误，不拖垮整批
        retries = max(1, int(self.retry_window_s / _GET_RETRY_S))
        for attempt in range(retries + 1):
            try:
                r = await self._c.request(method, path, **kw)
                break
            except httpx.TransportError as exc:
                if (method != "GET" and not retry) or attempt == retries:
                    raise AtlasError(f"{method} {path} → 连接失败：{exc!r}") from exc
                await asyncio.sleep(_GET_RETRY_S)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.json().get("message") or r.text[:300]
            except ValueError:
                detail = r.text[:300]
            raise AtlasError(f"{method} {path} → {r.status_code}：{detail}", status=r.status_code)
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
        ops: ClusterOps | None = None,
    ) -> TrialRun:
        """ops：集群操作（任务带故障动作时要有，没有的话动作记为没做成）。"""
        thread = await self._req(
            "POST",
            "/v1/threads",
            json={
                "agent_id": agent["id"],
                # ★ 给了标题就算用户起的（manual），自动生成永不覆盖：查自动标题的题不给
                "title": "" if task.title_within_s is not None else f"bench:{task.id}#{trial}",
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
                content=content.encode("utf-8") if isinstance(content, str) else content,
            )
        faults = FaultInjector(task.actions, ops, _TrialSite(self, tr)) if task.actions else None
        listener = asyncio.create_task(
            self._approve_from_stream(
                tr, approval_policy, task.sse_disconnect_after, task.user, answerer, faults
            )
        )
        #: 每一轮发出的时刻（monotonic）：首个干活事件要等流收全再对
        sent_at: dict[str, float] = {}
        try:
            if faults is not None:
                faults.start()
            for n, (text, wait_s) in enumerate(
                [(task.instruction, 0.0), *((t.text, t.wait_s) for t in task.turns)]
            ):
                if n:
                    if faults is not None:
                        await faults.settle()  # 上一轮触发的故障做完再发下一轮
                    if tr.budget_exceeded or wait_s > self._budget_left(tr, task):
                        break
                    await asyncio.sleep(wait_s)
                sent = time.monotonic()
                accepted = await self._req(
                    "POST",
                    f"/v1/threads/{tr.thread_id}/runs",
                    json={"content": [{"type": "text", "text": text}]},
                    headers={"Idempotency-Key": f"bench-{uuid.uuid4().hex}"},
                )
                tr.run_ids.append(accepted["run_id"])
                tr.run_id = tr.run_id or accepted["run_id"]
                sent_at[accepted["run_id"]] = sent
                await self._wait(tr, task, accepted["run_id"], n + 1, sent)
                if tr.budget_exceeded:
                    break
            if task.title_within_s is not None and not tr.budget_exceeded:
                await self._await_title(tr, task.title_within_s)
            await self._drain(tr)
            for turn in tr.turns:
                first = tr.first_work.get(turn["run_id"])
                if first is not None:
                    turn["first_event_s"] = round(first - sent_at[turn["run_id"]], 1)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
            if faults is not None:
                await faults.close()
                tr.actions, tr.actions_pending = faults.records, faults.pending()
        return tr

    @staticmethod
    def _budget_left(tr: TrialRun, task: Task) -> float:
        return task.budget.wall_s - (time.monotonic() - tr.started)

    async def _wait(self, tr: TrialRun, task: Task, run_id: str, n: int, sent: float) -> None:
        """等第 n 轮到终态。多轮时 status 是最后一轮的；错误记第一个预期之外失败的那一轮（被取消的
        不算 —— 取消是用例自己发起的动作，或超预算；任务声明了预期失败的也不算）。"""
        while True:
            run = await self._req("GET", f"/v1/runs/{run_id}")
            tr.status = status = run["status"]
            if status in _TERMINAL:
                kind, message = run.get("error_kind"), run.get("error_message")
                expect = task.run_outcomes.get(n) or RunExpect()
                expected = expect.matches(status, kind, message)
                tr.turns.append(
                    {
                        "n": n,
                        "run_id": run_id,
                        "status": status,
                        "error_kind": kind,
                        "error": (message or "")[:300],
                        "expected": expected,
                        "wall_s": round(time.monotonic() - sent, 1),
                        # 发出到第一条干活的事件：收尾时等流收全再填（见 run_trial）
                        "first_event_s": None,
                        "cancel_to_end_s": (
                            round(time.monotonic() - tr.cancelled_at[run_id], 1)
                            if run_id in tr.cancelled_at
                            else None
                        ),
                    }
                )
                if status in ("failed", "interrupted") and tr.error is None and not expected:
                    tr.error_kind = kind or status
                    tr.error = f"{tr.error_kind}：{message or ''}"
                return
            elapsed = time.monotonic() - tr.started
            over = None
            if elapsed > task.budget.wall_s:
                over = f"超过时间预算 {task.budget.wall_s:.0f}s"
            elif int(run.get("total_tokens") or 0) > task.budget.max_tokens:
                over = f"超过 token 预算 {task.budget.max_tokens}"
            if over:
                tr.budget_exceeded = over
                wall = round(time.monotonic() - sent, 1)
                tr.turns.append({"n": n, "run_id": run_id, "status": "budget_exceeded",
                                 "expected": False, "wall_s": wall})  # fmt: skip
                with contextlib.suppress(AtlasError):
                    await self._req("POST", f"/v1/runs/{run_id}/cancel")
                return
            await asyncio.sleep(_POLL_S)

    async def _await_title(self, tr: TrialRun, within_s: float) -> None:
        """等会话的标题生成出来（来源不再是 pending），最多 within_s 秒。"""
        until = time.monotonic() + within_s
        while True:
            thread = await self._req("GET", f"/v1/threads/{tr.thread_id}")
            tr.title_source = str(thread.get("title_source") or "")
            if tr.title_source != "pending" or time.monotonic() > until:
                return
            await asyncio.sleep(_POLL_S)

    async def _drain(self, tr: TrialRun) -> None:
        """等流上出现各轮的终止事件：轮询 GET 先看到终态时，流上的最后几条事件可能还没读到。"""
        until = time.monotonic() + _DRAIN_S
        while not set(tr.run_ids) <= tr.stream_ended and time.monotonic() < until:
            await asyncio.sleep(0.2)

    async def _approve_from_stream(
        self,
        tr: TrialRun,
        policy: str,
        disconnect_after: int | None = None,
        user: SimulatedUser | None = None,
        answerer: Answerer | None = None,
        faults: FaultInjector | None = None,
    ) -> None:
        """跟着会话事件流批审批（主 run 与子 run 的都在这条流上）。断了就带游标重连。
        每条事件也交给故障注入器看是否到了触发时机。

        disconnect_after：收到这么多条事件后**主动**断开、2 秒后带游标重连 ——
        检查断线续传有没有丢事件或重复（E18）。只断一次。
        """
        cursor = 0
        decided: set[str] = set()
        approvals = 0
        delay = float(policy.split(":", 1)[1]) if policy.startswith("delay:") else 0.0
        #: 审批的决定另起任务提交：等人（delay）、断线重试时不耽误读流 —— 流上的事件要全部留下，
        #: 故障也要按时触发
        submitting: set[asyncio.Task[None]] = set()
        try:
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
                            "GET",
                            f"/v1/threads/{tr.thread_id}/events",
                            params={"after_seq": cursor},
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
                            if not isinstance(event, dict):
                                continue
                            _keep(tr, event)
                            if faults is not None:
                                faults.observe(event)
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
                            decision = _decide(policy, approvals)
                            approvals += 1
                            task = asyncio.create_task(
                                self._approve(tr, event, data, decision, delay)
                            )
                            submitting.add(task)
                            task.add_done_callback(submitting.discard)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 断线：稍后带游标重连
                    logger.debug("会话事件流断开：%s", exc)
                # 流结束（正常不会）或断开：都稍等再带游标重连，不空转
                await asyncio.sleep(2.0 if tr.sse_reconnects else 1.0)
        finally:
            for task in submitting:
                task.cancel()

    async def _approve(
        self,
        tr: TrialRun,
        event: dict[str, Any],
        data: dict[str, Any],
        decision: str,
        delay: float,
    ) -> None:
        """提交一次审批决定。断线时重试（同一条审批再提交一次是安全的：已处理的回 409）。"""
        t0 = time.monotonic()
        if delay:
            # 模拟「人很久之后才批」：平台不该因为等待而结束 run
            await asyncio.sleep(delay)
        outcome = decision
        try:
            await self._req(
                "POST",
                f"/v1/runs/{event['run_id']}/approvals/{data['approval_id']}",
                json={"decision": decision},
                retry=True,
            )
        except AtlasError as exc:
            # 409：已被处理过或已作废（比如等待期间 run 被取消了）—— 这次决定没有生效
            outcome = "conflict" if exc.status == 409 else "error"
            logger.info("审批 %s 的决定没有生效：%s", data["approval_id"], exc)
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        tr.approvals.append(
            {
                "approval_id": str(data["approval_id"]),
                "run_id": str(event.get("run_id") or ""),
                "tool": data.get("tool_name"),
                # 命令：按计数标记数审批次数（接管后不该再问一次）。CLI 的在 toolCall.rawInput 里
                "command": command_of(args),
                # CLI 的工具类别（ACP ToolKind：execute / edit / read…）：被请示的是命令还是写入
                "kind": str(args.get("kind") or ""),
                "decision": outcome,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        )

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
        # 前置会话里的命令同样要审批：另起一个监听，全部批准（前置会话不是被测对象）
        setup_tr = TrialRun(thread_id=thread["id"])
        listener = asyncio.create_task(self._approve_from_stream(setup_tr, "approve_all"))
        try:
            await self._setup_turns(task, thread["id"], deadline, tr)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        if tr.setup_error:
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

    async def _setup_turns(self, task: Task, thread_id: str, deadline: float, tr: TrialRun) -> None:
        """前置会话里按顺序发 setup 消息，各等到终态；失败或超时记进 tr.setup_error。"""
        for text in task.setup:
            accepted = await self._req(
                "POST",
                f"/v1/threads/{thread_id}/runs",
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
        trace: list[dict[str, Any]] = []
        for rid in tr.run_ids or [tr.run_id]:  # 多轮：每一轮的轨迹（各自含子 run）按顺序拼起来
            trace.extend((await self._req("GET", f"/v1/runs/{rid}/trace"))["data"])
        seen = [*tr.run_ids, tr.run_id, *(str(e["run_id"]) for e in trace)]
        run_ids = list(dict.fromkeys(seen))
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

    async def sandbox_host(self, tr: TrialRun, mark: str | None) -> str:
        """会话正在用的沙箱 Pod 名：计数脚本的 start 文件里记着沙箱的 hostname（就是 Pod 名）。

        mark 为空时取任意一个标记最新的 start 文件。刚触发时文件可能还没写出来，等一会儿。
        """
        prefix = f"{MARK_DIR}/{mark}/start-" if mark else f"{MARK_DIR}/"
        deadline = time.monotonic() + _MARK_WAIT_S
        while True:
            listing = await self._req("GET", f"/v1/threads/{tr.thread_id}/files")
            starts = sorted(
                f["path"]
                for f in listing.get("data") or []
                if f["path"].startswith(prefix) and "/start-" in f["path"]
            )
            if starts:
                r = await self._c.get(
                    f"/v1/threads/{tr.thread_id}/files/download", params={"path": starts[-1]}
                )
                for part in r.text.split():
                    if part.startswith("host="):
                        return part.removeprefix("host=")
            if time.monotonic() > deadline:
                raise AtlasError(f"{_MARK_WAIT_S:.0f}s 内没等到 {prefix}* 写出来：找不到沙箱 Pod")
            await asyncio.sleep(1.0)

    async def cancel_run(self, run_id: str) -> None:
        await self._req("POST", f"/v1/runs/{run_id}/cancel")

    async def pending_approvals(self, tr: TrialRun) -> int:
        """各轮结束后还挂着的审批（取消、接管之后应当都作废或处理掉了）。"""
        left = 0
        for rid in tr.run_ids:
            with contextlib.suppress(AtlasError):
                left += len((await self._req("GET", f"/v1/runs/{rid}/approvals")).get("data") or [])
        return left

    async def sandbox_exec(
        self,
        tr: TrialRun,
        command: str,
        *,
        cwd: str,
        timeout_s: float,
        files: dict[str, bytes] | None = None,
    ) -> dict[str, Any] | None:
        """在会话正在运行的评测档沙箱里执行一条命令（sandbox 模式的校验）。

        → {exit_code, output, truncated}；None = 会话没有正在运行的沙箱（平台不会为它新建）。
        files：执行前写进沙箱的 {绝对路径: 内容}。
        """
        body = {
            "command": command,
            "cwd": cwd,
            "timeout_s": int(timeout_s),
            "files": [
                {"path": path, "content_b64": base64.b64encode(data).decode()}
                for path, data in (files or {}).items()
            ],
        }
        # 请求一直挂到命令结束：超时要盖过命令自己的上限
        try:
            r = await self._c.post(
                f"/v1/threads/{tr.thread_id}/sandbox/exec", json=body, timeout=timeout_s + 120
            )
        except httpx.TransportError as exc:  # 不重试：校验脚本会再跑一遍
            raise AtlasError(f"POST sandbox/exec → 连接失败：{exc!r}") from exc
        if r.status_code >= 400:
            try:
                error = r.json().get("error") or {}
            except ValueError:
                error = {}
            if error.get("kind") == "sandbox_not_running":
                return None
            detail = error.get("message") or r.text[:300]
            raise AtlasError(f"POST sandbox/exec → {r.status_code}：{detail}")
        return r.json()

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


@dataclass
class _TrialSite:
    """故障注入器看到的现场（faults.TrialContext）：当前的 run、会话、会话的沙箱 Pod、取消 run。"""

    platform: AtlasAgentPlatform
    tr: TrialRun

    def current_run(self) -> str:
        return self.tr.run_ids[-1] if self.tr.run_ids else self.tr.run_id

    def session(self) -> str:
        return self.tr.thread_id

    async def sandbox_pod(self, mark: str | None) -> str:
        return await self.platform.sandbox_host(self.tr, mark)

    async def cancel_run(self, run_id: str) -> None:
        self.tr.cancelled_at.setdefault(run_id, time.monotonic())
        await self.platform.cancel_run(run_id)
