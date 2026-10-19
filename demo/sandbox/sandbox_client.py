"""PodSandbox：SandboxProtocol 的 K8s Pod 实现（server 侧，对应 providers/sandbox.py）。

    首次 execute   经 cluster 确保 Pod 在跑（按需创建：多数会话用不到沙箱，建会话时不建 Pod）
    每次 execute   POST sandboxd /v1/execs，exec_id = {run_id}:{tool_call_id}，读 NDJSON 直到 exited
    流断了         重新 ensure（Pod 可能换了地址）→ 再 POST 同一个 exec_id，
                   带 after 和见过的 boot_id
                   → 没开始就开始、开始了就接上；Pod 重建过 → 409，不重跑
    这一轮被取消   通知 sandboxd 结束这条命令的进程组
    Pod 重建过     boot_id 变了 → 在工具结果前提示「环境已重建」

★ 失败一律抛 SandboxUnavailable：中间件转成工具错误回给模型，不让整轮失败。
★ 不设命令时长上限：读超时只用来发现「连接断了」（sandboxd 每 HEARTBEAT_S 发一次心跳），断了就接上。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
from agent import ExecuteResponse, SandboxUnavailable
from cluster import ClusterUnavailable, PodInfo, PodManager
from sandboxd import SESSION_HEADER
from tracing import say

READ_TIMEOUT_S = 1.0  # 大于 sandboxd 的心跳间隔：超过它 = 连接死了，不是命令慢。Atlas 是 60 秒
RECONNECT_ATTEMPTS = 5  # Atlas 是 15 次
BACKOFF_S = 0.05  # 第 n 次重连前等 BACKOFF_S × 2^(n-1)。Atlas 从 1 秒开始，封顶 10 秒
SHOW_EVENTS = 3  # 每条命令的 output / heartbeat 各打印前几条


class _Gone(Exception):
    """sandboxd 不认识这个 exec，而且它是重建过的 —— 命令在执行期间随 Pod 一起没了。"""


class PodSandbox:
    """一个 run 一个实例；同一工作区的多个实例指向同一个 Pod。"""

    def __init__(
        self,
        *,
        cluster: PodManager,
        workspace_thread_id: str,
        user_id: str,
        run_id: str,
        boots: dict[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cluster = cluster
        self._ws = workspace_thread_id
        self._user = user_id
        self._run = run_id
        # 每个工作区最近一次见到的 boot_id。进程级（真实代码是模块级字典）：变了 = Pod 重建过
        self._boots = boots
        self._transport = transport
        self._endpoint: PodInfo | None = None
        self._background: set[asyncio.Task] = set()

    @property
    def id(self) -> str:
        return f"atlas-sbx-{self._ws}"

    async def aexecute(
        self, command: str, *, timeout: int | None = None, exec_id: str | None = None
    ) -> ExecuteResponse:
        # ★ exec_id 由 (run, tool_call) 派生：同一个调用被重放时还是这个 id，
        #   sandboxd 据此接上而不是重跑
        eid = f"{self._run}:{exec_id or uuid4().hex[:8]}"
        body: dict[str, Any] = {"exec_id": eid, "run_id": self._run, "command": command}
        if timeout:
            # 沙箱内部计时、结束进程组：只在客户端放弃的话，命令会一直跑
            body["timeout_s"] = timeout
        try:
            result, boot_id = await self._run_exec(eid, body)
        except asyncio.CancelledError:
            # 这一轮被取消：结束沙箱里的命令（发出去就走，不等它回来）
            say("PodSandbox", f"{eid} 这一轮被取消 → POST /v1/execs/{eid}/kill")
            task = asyncio.get_running_loop().create_task(self._kill(eid))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            raise
        text = str(result.get("output") or "")
        if notes := result.get("notes"):
            text += "\n" + "\n".join(f"[{n}]" for n in notes)
        if notice := self._boot_notice(boot_id):
            say("PodSandbox", f"{eid} boot 变了 → 工具结果前加一句「环境已重建」")
            text = f"{notice}\n{text}"
        return ExecuteResponse(text, result.get("exit_code"), bool(result.get("truncated")))

    # ------------------------------------------------------------------ 内部

    def _boot_notice(self, boot_id: str) -> str:
        previous = self._boots.get(self._ws)
        self._boots[self._ws] = boot_id
        if previous and previous != boot_id:
            return "[沙箱环境已重建：/workspace 内的文件仍在；装进 HOME 的依赖、后台进程已丢失]"
        return ""

    async def _ensure(self, *, fresh: bool = False) -> PodInfo:
        if self._endpoint is not None and not fresh:
            return self._endpoint
        try:
            self._endpoint = await self._cluster.ensure(
                workspace_thread_id=self._ws, user_id=self._user
            )
        except ClusterUnavailable as exc:
            raise SandboxUnavailable(f"沙箱启动失败：{exc}") from exc
        return self._endpoint

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, read=READ_TIMEOUT_S),
            trust_env=False,  # 不走代理：Pod IP 是集群内地址
            transport=self._transport,
        )

    def _headers(self, ep: PodInfo) -> dict[str, str]:
        return {"Authorization": f"Bearer {ep.token}", SESSION_HEADER: self._ws}

    async def _run_exec(self, eid: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """发起并读到 exited 为止；流断了就接上。返回 (exited 事件, boot_id)。"""
        state: dict[str, Any] = {"seq": 0, "boot": "", "shown": {}}
        attempt = 0
        while True:
            # ★ 首发和重连都是 POST 同一个 exec_id（幂等）：没开始就开始，开始了就接上。
            #   见过 boot_id 之后带上它 —— Pod 重建过的话 sandboxd 回 409，而不是再跑一遍
            req = {**body, "after": state["seq"]}
            if state["boot"]:
                req["expect_boot_id"] = state["boot"]
            try:
                if self._endpoint is None:
                    say("PodSandbox", f"{eid} 这个 run 还没拿过 Pod 地址 → cluster.ensure")
                ep = await self._ensure(fresh=attempt > 0)
                if attempt == 0:
                    say("PodSandbox", f"POST {ep.url}/v1/execs  exec_id={eid}")
                result = await self._stream(ep, eid, req, state)
                if result is not None:
                    return result, state["boot"]
                say("PodSandbox", f"{eid} 流在 exited 之前结束了 → 当作断线")
            except _Gone as exc:
                raise SandboxUnavailable(
                    "沙箱在命令执行期间重启（可能内存不足或被回收），命令已中断、结果丢失；"
                    "/workspace 内已写入的文件仍在"
                ) from exc
            except httpx.TransportError as exc:
                say("PodSandbox", f"{eid} 连接断了（{type(exc).__name__}）")
            attempt += 1
            if attempt > RECONNECT_ATTEMPTS:
                raise SandboxUnavailable("与沙箱的连接中断且无法恢复，命令结果未知")
            await asyncio.sleep(BACKOFF_S * 2 ** (attempt - 1))
            boot = f"、expect_boot_id={state['boot']}" if state["boot"] else ""
            say(
                "PodSandbox",
                f"{eid} 第 {attempt} 次重连：重新 ensure，"
                f"POST 同一个 exec_id（after={state['seq']}{boot}）",
            )

    async def _stream(
        self, ep: PodInfo, eid: str, req: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with (
            self._client() as client,
            client.stream(
                "POST", f"{ep.url}/v1/execs", json=req, headers=self._headers(ep)
            ) as resp,
        ):
            if resp.status_code == 409:
                say("PodSandbox", f"{eid} 409：Pod 重建过，这条命令已经没了 → 不重跑")
                raise _Gone(eid)
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")[:200]
                raise SandboxUnavailable(f"沙箱返回 {resp.status_code}：{text}")
            state["boot"] = resp.headers.get("X-Sandbox-Boot-Id", state["boot"])
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if "seq" in event:
                    state["seq"] = event["seq"]
                self._show(eid, event, state["shown"])
                if event["t"] == "exited":
                    return event
        return None

    def _show(self, eid: str, event: dict[str, Any], shown: dict[str, int]) -> None:
        kind = event["t"]
        if kind == "flushing" or (kind == "heartbeat" and "flushing" in shown):
            shown["flushing"] = 1  # 命令已退出、在等写回：barrier 那边会说，这里不重复
            return
        if kind in ("output", "heartbeat"):
            shown[kind] = shown.get(kind, 0) + 1
            if shown[kind] > SHOW_EVENTS:
                return
        head = f"{eid} #{event.get('seq', '')}"
        if kind == "started":
            say("PodSandbox", f"{head} started：pid {event['pid']}，boot {event['boot_id']}")
        elif kind == "output":
            first = event["data"].strip().split("\n")[0][:40]
            more = "（后面的输出不再逐条打印）" if shown[kind] == SHOW_EVENTS else ""
            say("PodSandbox", f"{head} output：{first}{more}")
        elif kind == "heartbeat":
            cpu, n = event["cpu_s"], event["output_bytes"]
            say("PodSandbox", f"{eid}    heartbeat：进程组 CPU {cpu:.2f} 秒，输出 {n} 字节")
        elif kind == "exited":
            flags = [k for k in ("truncated", "stalled", "timed_out", "killed") if event.get(k)]
            extra = f"，{'、'.join(flags)}" if flags else ""
            code, flushed = event["exit_code"], event["flushed"]
            say("PodSandbox", f"{head} exited：exit_code={code}{extra}，flushed={flushed}")

    async def _kill(self, eid: str) -> None:
        if (ep := self._endpoint) is None:
            return
        try:
            async with self._client() as client:
                await client.post(f"{ep.url}/v1/execs/{eid}/kill", headers=self._headers(ep))
        except httpx.HTTPError as exc:
            say("PodSandbox", f"取消沙箱命令 {eid} 失败：{exc}")
