"""原生智能体的 Shell 沙箱 —— SandboxProtocol 的 OpenSandbox 实现。

doc/sandbox-opensandbox-design.html §8。

    首次 execute   经 SandboxProvisioner 确保工作区的沙箱在跑
                   （按需建；子智能体与父会话共用，键是工作区）
    提交           execd 后台模式 POST /command → init 事件里的命令 id；
                   进程内记 {run}:{tool_call} → 命令 id
    重放           同一个调用再来一次 → 用记下的命令 id 从游标 0 重读
                   （跑完了就直接拿到原结果），不重跑
    跟随           轮询 GET logs?cursor= 与 GET status，直到退出；不设总时长上限
    断线 / 换 IP   重新 ensure，用同一个命令 id、同一个游标接上 —— 命令属于 execd，不属于连接
    沙箱换了       新沙箱的 execd 不认识这个 id → 工具错误「执行期间沙箱重启」，不重跑
    卡死           连续 idle_s 没有新输出、且这段时间容器 CPU 一直很低 → 中断，结果里写明
    取消           DELETE /command（execd 杀整个进程组）
    沙箱 id 变了   在工具结果前提示「环境已重建」

★ 失败一律抛 SandboxUnavailable：中间件把它转成工具错误回给模型，不让整轮失败。
★ 命令映射在进程内存里：跨进程接管时按横向扩展设计补「结果未知」（§9.4），原命令在沙箱里继续跑完。
★ 写穿式挂载下，命令退出时它写的文件已经落盘 —— 不再需要旧方案的「执行后等写回」（§10）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from atlas_engine.contracts import ExecuteResponse, SandboxUnavailable

from ..telemetry import tracer
from .opensandbox import ExecdClient, ExecdGone, ImageDenied, LifecycleError, QuotaExceeded

if TYPE_CHECKING:
    from .opensandbox import SandboxEndpoint, SandboxProvisioner

__all__ = ["PodSandbox"]

logger = logging.getLogger(__name__)

WORKDIR = "/workspace"
#: 断线后重新接上的尝试次数（指数退避，封顶 10 秒；合计约两分钟）
_RECONNECT_ATTEMPTS = 15
#: 卡死候选期间多久取一次容器 CPU
_CPU_SAMPLE_S = 15.0

#: 进程内存：{run}:{tool_call} → (沙箱 id, 命令 id)。同一个调用被重放时接上它，而不是再跑一遍。
#: 只记 id 不记输出（输出在 execd 里保留 24 小时），所以可以多记一些
_EXECS: OrderedDict[str, tuple[str, str]] = OrderedDict()
_EXECS_MAX = 4096
#: 每个工作区上次见到的沙箱。变了 = 环境重建过，要告诉模型
_SEEN: dict[str, str] = {}
#: 取消时发出的中断请求（持有引用，防止任务被回收）
_BACKGROUND: set[asyncio.Task[Any]] = set()


class _Gone(Exception):
    """命令所在的沙箱已经不是原来那个了。"""


@dataclass
class _Output:
    """边读边保留头尾：命令输出多大都不打爆内存。"""

    limit: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total: int = 0

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        room = self.limit // 2 - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail = (self.tail + chunk)[-(self.limit - self.limit // 2) :]

    def text(self) -> tuple[str, bool]:
        kept = len(self.head) + len(self.tail)
        head = bytes(self.head).decode(errors="replace")
        tail = bytes(self.tail).decode(errors="replace")
        if self.total <= kept:
            return head + tail, False
        return f"{head}\n…（省略中间 {self.total - kept} 字节）…\n{tail}", True


@dataclass
class _Exec:
    sandbox_id: str
    cmd_id: str
    out: _Output
    cursor: int = 0
    notes: list[str] = field(default_factory=list)


class PodSandbox:
    """一个 run 一个实例；同一工作区的多个实例指向同一个沙箱。"""

    def __init__(
        self,
        *,
        provisioner: SandboxProvisioner,
        workspace_thread_id: str,
        user_id: str,
        run_id: str,
        image: str = "",
        poll_min_s: float = 0.3,
        poll_max_s: float = 2.0,
        output_limit: int = 100_000,
        idle_s: float | None = None,
        stall_cpu_pct: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._p = provisioner
        self._ws, self._user, self._run, self._image = workspace_thread_id, user_id, run_id, image
        self._poll_min, self._poll_max = poll_min_s, poll_max_s
        self._limit = output_limit
        self._idle_s = idle_s
        self._stall_pct = stall_cpu_pct
        self._transport = transport

    @property
    def id(self) -> str:
        return f"sbx:{self._ws}"

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
        key = f"{self._run}:{exec_id}" if exec_id else ""
        with tracer().start_as_current_span("sandbox.exec") as span:
            span.set_attribute("atlas.sandbox.workspace", self._ws)
            started = time.monotonic()
            ep = await self._ensure()
            notice = self._notice(ep)
            submitted = _EXECS.get(key) if key else None
            if submitted is None:
                ex = await self._submit(ep, command, cwd or WORKDIR, timeout)
                if key:
                    _EXECS[key] = (ex.sandbox_id, ex.cmd_id)
                    while len(_EXECS) > _EXECS_MAX:
                        _EXECS.popitem(last=False)
            else:
                logger.info("%s 已提交过（%s）→ 接上，不重跑", key, submitted[1])
                ex = _Exec(*submitted, _Output(self._limit))
            span.set_attribute("atlas.sandbox.id", ex.sandbox_id)
            span.set_attribute("atlas.sandbox.cmd_id", ex.cmd_id)
            try:  # ★ 拿到命令 id 之后的每个 await 都可能被取消：都要在这个 try 里
                await self._p.routes.started(self._ws, ex.sandbox_id, ex.cmd_id)
                if ex.sandbox_id != ep.sandbox_id:
                    raise _Gone(ex.cmd_id)  # 重放时沙箱已经换过
                exit_code = await self._follow(ex, ep)
            except _Gone as exc:
                raise SandboxUnavailable(
                    "命令执行期间沙箱重启（可能内存不足或被回收），命令已中断、结果丢失；"
                    "/workspace 里已经写入的文件仍在"
                ) from exc
            except asyncio.CancelledError:
                self._interrupt_later(ep, ex.cmd_id)
                _EXECS.pop(key, None)  # 已经中断：再来一次就是新的执行
                raise
            finally:
                await self._p.routes.finished(self._ws, ex.sandbox_id, ex.cmd_id)
            span.set_attribute("atlas.sandbox.exit_code", -1 if exit_code is None else exit_code)
            span.set_attribute(
                "atlas.sandbox.duration_ms", int((time.monotonic() - started) * 1000)
            )
            span.set_attribute("atlas.sandbox.output_bytes", ex.out.total)
        text, truncated = ex.out.text()
        if ex.notes:
            text += "\n" + "\n".join(f"[{n}]" for n in ex.notes)
        return ExecuteResponse(output=notice + text, exit_code=exit_code, truncated=truncated)

    # ------------------------------------------------------------------ 内部

    async def _ensure(self, *, fresh: bool = False) -> SandboxEndpoint:
        with tracer().start_as_current_span("sandbox.ensure"):
            try:
                return await self._p.ensure(
                    self._ws, user=self._user, image=self._image, fresh=fresh
                )
            except (QuotaExceeded, ImageDenied, LifecycleError) as exc:
                raise SandboxUnavailable(f"沙箱启动失败：{exc}") from exc

    def _notice(self, ep: SandboxEndpoint) -> str:
        previous = _SEEN.get(self._ws)
        _SEEN[self._ws] = ep.sandbox_id
        if previous and previous != ep.sandbox_id:
            return (
                "[沙箱环境已重建：/workspace 内的文件仍在；安装的软件包、HOME 与 /repos 里的内容、"
                "后台进程已丢失]\n"
            )
        return ""

    def _execd(self, ep: SandboxEndpoint) -> ExecdClient:
        return ExecdClient(ep.url, ep.token, transport=self._transport)

    async def _submit(
        self, ep: SandboxEndpoint, command: str, cwd: str, timeout: int | None
    ) -> _Exec:
        start = asyncio.ensure_future(self._execd(ep).start(command, cwd=cwd, timeout_s=timeout))
        try:
            cmd_id = await asyncio.shield(start)
        except asyncio.CancelledError:
            # 取消落在提交途中：请求可能已经到了 execd。等它拿到 id 再中断，免得命令在沙箱里没人管
            start.add_done_callback(lambda t: self._interrupt_submitted(ep, t))
            raise
        except httpx.RemoteProtocolError as exc:
            raise SandboxUnavailable(
                "提交命令时连接中断：命令可能已经开始执行，结果未知；"
                "可以先检查 /workspace 的状态再决定是否重试"
            ) from exc
        except httpx.HTTPError as exc:
            self._p.routes.forget_cached(self._ws)
            raise SandboxUnavailable(f"沙箱拒绝了命令：{exc}") from exc
        return _Exec(ep.sandbox_id, cmd_id, _Output(self._limit))

    async def _follow(self, ex: _Exec, ep: SandboxEndpoint) -> int | None:
        delay, attempts = self._poll_min, 0
        quiet_since = time.monotonic()
        cpu_samples: list[float] = []
        next_sample = 0.0
        while True:
            try:
                execd = self._execd(ep)
                chunk, ex.cursor = await execd.logs(ex.cmd_id, ex.cursor)
                ex.out.add(chunk)
                status = await execd.status(ex.cmd_id)
                if not status.get("running"):
                    chunk, ex.cursor = await execd.logs(ex.cmd_id, ex.cursor)  # 收尾
                    ex.out.add(chunk)
                    if status.get("error") and status.get("exit_code") is None:
                        ex.notes.append(f"命令没能启动：{status['error']}")
                    return status.get("exit_code")
                attempts = 0
                now = time.monotonic()
                if chunk:
                    quiet_since, cpu_samples, delay = now, [], self._poll_min
                else:
                    delay = min(delay * 2, self._poll_max)
                if self._idle_s and now - quiet_since >= self._idle_s and now >= next_sample:
                    next_sample = now + _CPU_SAMPLE_S
                    if await self._stalled(execd, cpu_samples, now - quiet_since):
                        ex.notes.append(
                            f"命令连续 {int(now - quiet_since)} 秒既无输出、也无 CPU 活动，"
                            "判定为卡死，已结束"
                        )
                        await execd.interrupt(ex.cmd_id)
                await asyncio.sleep(delay)
            except ExecdGone as exc:
                raise _Gone(ex.cmd_id) from exc
            except httpx.TransportError as exc:
                attempts += 1
                logger.info("命令 %s 的连接中断（第 %d 次）：%s", ex.cmd_id, attempts, exc)
                if attempts > _RECONNECT_ATTEMPTS:
                    raise SandboxUnavailable("与沙箱的连接中断且无法恢复，命令结果未知") from exc
                await asyncio.sleep(min(2 ** (attempts - 1), 10))
                ep = await self._ensure(fresh=True)  # Pod 可能换了 IP，也可能整个换了
                if ep.sandbox_id != ex.sandbox_id:
                    raise _Gone(ex.cmd_id) from exc

    async def _stalled(self, execd: ExecdClient, samples: list[float], quiet_s: float) -> bool:
        """无输出已超过 idle_s：再看容器 CPU。取不到 CPU 就不下结论（宁可不杀）。"""
        pct = await execd.cpu_pct()
        if pct is None:
            return False
        samples.append(pct)
        if pct >= self._stall_pct:
            samples.clear()
            return False
        # 至少两次采样都很低（间隔 _CPU_SAMPLE_S）才判：一次低可能只是恰好在等 IO
        return len(samples) >= 2 and quiet_s >= (self._idle_s or 0) + _CPU_SAMPLE_S

    def _interrupt_submitted(self, ep: SandboxEndpoint, start: asyncio.Future[str]) -> None:
        if start.cancelled() or start.exception() is not None:
            return  # 没提交成功：没有要中断的
        self._interrupt_later(ep, start.result())

    def _interrupt_later(self, ep: SandboxEndpoint, cmd_id: str) -> None:
        """这一轮被取消：结束沙箱里的命令（不等它回来）。"""

        async def interrupt() -> None:
            try:
                await self._execd(ep).interrupt(cmd_id)
            except httpx.HTTPError:
                logger.warning("取消沙箱命令 %s 失败", cmd_id, exc_info=True)

        task = asyncio.get_running_loop().create_task(interrupt())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
