"""原生智能体的 Shell 沙箱 —— SandboxProtocol 的 K8s Pod 实现（doc/native-sandbox-design.html）。

    首次 execute     经 cluster 确保沙箱 Pod 在跑（按需创建；子智能体与父会话共用，键是工作区）
    每次 execute     POST sandboxd /v1/execs（流式 NDJSON），exec_id = {run_id}:{tool_call_id}
    流断了           重新 ensure（Pod 可能换了 IP）→ 再 POST 同一个 exec_id
                     （带 after 与见过的 boot_id）→ 接上原命令；Pod 重建过则 409，不重跑
    这一轮被取消     通知 sandboxd 结束该命令的进程组
    Pod 被重建       boot_id 变了 → 在工具结果前提示「环境已重建」

★ 失败一律抛 SandboxUnavailable：中间件把它转成工具错误回给模型，不让整轮失败（§2）。
★ 不设命令时长上限（§11）：读超时只用来发现「连接断了」（sandboxd 每 15 秒一次心跳），
  断了就接上；只有连续连不上 cluster / Pod 才放弃。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from atlas_engine.contracts import ExecuteResponse, SandboxUnavailable

from ..telemetry import tracer
from .cluster import ClusterUnavailable
from .pods import PodEndpoint

if TYPE_CHECKING:
    from .cluster import ClusterPods

__all__ = ["PodSandbox"]

logger = logging.getLogger(__name__)

SESSION_HEADER = "X-Atlas-Sandbox"
#: 读超时：大于 sandboxd 的心跳间隔（15s）。超过它 = 连接已死，不是命令慢
_READ_TIMEOUT_S = 60.0
#: 断线后重新接上的尝试次数（指数退避，封顶 10 秒；合计约两分钟）
_RECONNECT_ATTEMPTS = 15

#: 每个工作区最近一次见到的 sandboxd boot_id（进程级）。变了 = Pod 重建过，要告诉模型
_BOOTS: dict[str, str] = {}
#: 取消时发出的 kill 请求（持有引用，防止任务被回收）
_BACKGROUND: set[asyncio.Task[Any]] = set()


class _Gone(Exception):
    """sandboxd 不认识这个 exec —— Pod 在命令执行期间重启过。"""


class PodSandbox:
    """一个 run 一个实例；同一工作区的多个实例指向同一个 Pod。"""

    def __init__(
        self,
        *,
        pods: ClusterPods,
        workspace_thread_id: str,
        user_id: str,
        run_id: str,
        git_profile: str = "",
        image: str = "",
        idle_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._pods = pods
        self._ws = workspace_thread_id
        self._user = user_id
        self._run = run_id
        self._git = git_profile
        self._image = image
        self._idle_s = idle_s
        self._transport = transport
        self._endpoint: PodEndpoint | None = None

    @property
    def id(self) -> str:
        return f"atlas-sbx-{self._ws}"

    def execute(self, command: str, *, timeout: int | None = None, cwd: str | None = None) -> Any:
        raise NotImplementedError("沙箱只支持异步调用（aexecute）")

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        exec_id: str | None = None,
    ) -> ExecuteResponse:
        eid = f"{self._run}:{exec_id or uuid4().hex}"
        body: dict[str, Any] = {"exec_id": eid, "run_id": self._run, "command": command}
        if timeout:
            body["timeout_s"] = timeout
        if cwd:
            body["cwd"] = cwd
        if self._idle_s is not None:
            body["idle_s"] = self._idle_s

        with tracer().start_as_current_span("sandbox.exec") as span:
            span.set_attribute("atlas.sandbox.workspace", self._ws)
            span.set_attribute("atlas.sandbox.exec_id", eid)
            started = time.monotonic()
            try:
                result, boot_id = await self._run_exec(eid, body)
            except asyncio.CancelledError:
                # 这一轮被取消：结束沙箱里的命令（不等它回来）
                task = asyncio.get_running_loop().create_task(self._kill(eid))
                _BACKGROUND.add(task)
                task.add_done_callback(_BACKGROUND.discard)
                raise
            span.set_attribute("atlas.sandbox.exit_code", result.get("exit_code") or 0)
            span.set_attribute(
                "atlas.sandbox.duration_ms", int((time.monotonic() - started) * 1000)
            )
            for key in ("stalled", "timed_out", "killed", "truncated"):
                span.set_attribute(f"atlas.sandbox.{key}", bool(result.get(key)))
            if result.get("flushed") is not None:
                span.set_attribute("atlas.sandbox.flushed", bool(result["flushed"]))

        text = str(result.get("output") or "")
        if notes := [str(n) for n in result.get("notes") or []]:
            text += "\n" + "\n".join(f"[{n}]" for n in notes)
        if notice := self._boot_notice(boot_id):
            text = f"{notice}\n{text}"
        return ExecuteResponse(
            output=text,
            exit_code=result.get("exit_code"),
            truncated=bool(result.get("truncated")),
        )

    # ------------------------------------------------------------------ 内部

    def _boot_notice(self, boot_id: str) -> str:
        if not boot_id:
            return ""
        previous = _BOOTS.get(self._ws)
        _BOOTS[self._ws] = boot_id
        if previous and previous != boot_id:
            return (
                "[沙箱环境已重建：/workspace 内的文件仍在；安装的软件包、HOME 与 /repos 里的内容、"
                "后台进程已丢失]"
            )
        return ""

    async def _ensure(self, *, fresh: bool = False) -> PodEndpoint:
        if self._endpoint is not None and not fresh:
            return self._endpoint
        with tracer().start_as_current_span("sandbox.ensure"):
            try:
                self._endpoint = await self._pods.ensure_sandbox(
                    workspace_thread_id=self._ws,
                    user_id=self._user,
                    image=self._image,
                    git_profile=self._git,
                )
            except ClusterUnavailable as exc:
                raise SandboxUnavailable(f"沙箱启动失败：{exc}") from exc
        return self._endpoint

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, read=_READ_TIMEOUT_S),
            trust_env=False,
            transport=self._transport,
        )

    def _headers(self, ep: PodEndpoint) -> dict[str, str]:
        return {"Authorization": f"Bearer {ep.token}", SESSION_HEADER: self._ws}

    async def _run_exec(self, eid: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """发起并读到 exited 为止；流断了就接上。返回 (exited 事件, boot_id)。"""
        state: dict[str, Any] = {"seq": 0, "boot": ""}
        attempt = 0
        while True:
            # ★ 首发与重连都是 POST 同一个 exec_id（幂等）：没开始就开始，开始了就接上。
            #   见过 boot_id 之后带上它 —— Pod 重建过的话 sandboxd 回 409，而不是再跑一遍
            req = {**body, "after": state["seq"]}
            if state["boot"]:
                req["expect_boot_id"] = state["boot"]
            try:
                ep = await self._ensure(fresh=attempt > 0)
                result = await self._stream(ep, "POST", "/v1/execs", state, json_body=req)
                if result is not None:
                    return result, state["boot"]
            except _Gone as exc:
                raise SandboxUnavailable(
                    "沙箱在命令执行期间重启（可能内存不足或被回收），命令已中断、结果丢失；"
                    "/workspace 内已写入的文件仍在"
                ) from exc
            except httpx.TransportError as exc:
                logger.info("exec %s 的连接中断（第 %d 次）：%s", eid, attempt + 1, exc)
            attempt += 1
            if attempt > _RECONNECT_ATTEMPTS:
                raise SandboxUnavailable("与沙箱的连接中断且无法恢复，命令结果未知")
            await asyncio.sleep(min(2 ** (attempt - 1), 10))

    async def _stream(
        self,
        ep: PodEndpoint,
        method: str,
        path: str,
        state: dict[str, Any],
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with (
            self._client() as client,
            client.stream(
                method, f"{ep.url}{path}", json=json_body, headers=self._headers(ep)
            ) as resp,
        ):
            if resp.status_code == 409:
                raise _Gone(path)
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")[:300]
                raise SandboxUnavailable(f"沙箱返回 {resp.status_code}：{text}")
            state["boot"] = resp.headers.get("X-Sandbox-Boot-Id", state["boot"])
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if "seq" in event:
                    state["seq"] = event["seq"]
                if event.get("t") == "started" and event.get("boot_id"):
                    state["boot"] = event["boot_id"]
                if event.get("t") == "exited":
                    return event
        return None  # 流在 exited 之前结束：当作断线，接上

    async def _kill(self, eid: str) -> None:
        ep = self._endpoint
        if ep is None:
            return
        try:
            async with self._client() as client:
                await client.post(f"{ep.url}/v1/execs/{eid}/kill", headers=self._headers(ep))
        except httpx.HTTPError:
            logger.warning("取消沙箱命令 %s 失败", eid, exc_info=True)
