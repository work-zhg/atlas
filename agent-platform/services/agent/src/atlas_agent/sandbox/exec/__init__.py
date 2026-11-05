"""沙箱 · 命令执行（c4-model/agent/sandbox/code-sandbox-exec.html）：在工作区的沙箱里后台执行命令。

    ShellSandbox      实现引擎的 SandboxProtocol：确保沙箱 → 后台提交 → 跟随到退出；重放不重跑、断线
                      接上、卡死和取消时中断；失败一律抛 SandboxUnavailable
    exec_in_running   评测原地校验：只在已经在跑的评测档沙箱里执行，不新建
    ExecutionLedger   (run, 工具调用) → (sandbox_uuid, 命令 id)，Redis，24 小时过期
    PollingPolicy · StallDetector · CommandExecution · OutputBuffer

★ 只拿 sandbox_uuid：地址和令牌都在基座里。沙箱换了（SandboxGone）或重启过（CommandGone）如实报错、
  不重跑；连接断了用同一个 sandbox_uuid 接着读（基座重新解析地址）。
★ 活动不在这里记：基座执行在提交、命令结束、打开文件时自动记，命令执行不用回头通知原生沙箱。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from atlas_engine.contracts import ExecuteResponse, SandboxUnavailable

from ...domain.spec import SandboxProfile, SandboxSpec
from ...telemetry import tracer
from ..base.execution.ports import SandboxExec
from ..base.execution.values import (
    CommandGone,
    CommandSpec,
    ExecdRejected,
    ExecdUnreachable,
    LogChunk,
    ResourceUsage,
    SandboxGone,
    SubmitUnknown,
)
from ..native import NativeSandboxService, WorkspaceRef
from ..provisioning.model import SandboxRef

if TYPE_CHECKING:
    import redis.asyncio as aioredis

__all__ = [
    "WORKDIR",
    "CommandExecution",
    "ExecutionLedger",
    "OutputBuffer",
    "PollingPolicy",
    "SandboxNotRunning",
    "SandboxProfileDenied",
    "ShellSandbox",
    "StallDetector",
    "exec_in_running",
]

logger = logging.getLogger(__name__)

WORKDIR = "/workspace"
#: 取消时发出的中断请求（持有引用，防止任务被回收）
_BACKGROUND: set[asyncio.Task[Any]] = set()
#: 沙箱换了的提示：/workspace 还在，别的都没了
REBUILT = (
    "[沙箱环境已重建：/workspace 内的文件仍在；"
    "安装的软件包、HOME 与 /repos 里的内容、后台进程已丢失]\n"
)
_GONE = (
    "命令执行期间沙箱重启（可能内存不足或被回收），命令已中断、结果丢失；"
    "/workspace 里已经写入的文件仍在"
)


class SandboxNotRunning(RuntimeError):
    """工作区没有正在运行的沙箱。直接执行不新建沙箱。"""


class SandboxProfileDenied(RuntimeError):
    """沙箱的档位不允许直接执行。"""


class ExecutionLedger:
    """(run, tool_call) → (sandbox_uuid, cmd_id)，存在 Redis。

    24 小时过期，和 execd 保留输出的时长一致。

    同一个调用被重放时接上原来的命令，而不是再跑一遍 —— 接管的实例重放也接得上。另记每个工作区
    上次执行用的沙箱，用来提示环境已重建。
    """

    _TTL_S = 24 * 3600

    def __init__(self, redis: aioredis.Redis, *, prefix: str = "atlas:exec") -> None:
        self._r = redis
        self._prefix = prefix

    def _key(self, run: str, call: str) -> str:
        return f"{self._prefix}:{run}:{call}"

    async def get(self, run: str, call: str) -> tuple[UUID, str] | None:
        raw = await self._r.get(self._key(run, call))
        if not raw:
            return None
        sandbox_uuid, cmd_id = json.loads(raw)
        return UUID(str(sandbox_uuid)), str(cmd_id)

    async def put(self, run: str, call: str, sandbox_uuid: UUID, cmd_id: str) -> None:
        await self._r.set(
            self._key(run, call), json.dumps([str(sandbox_uuid), cmd_id]), ex=self._TTL_S
        )

    async def drop(self, run: str, call: str) -> None:
        await self._r.delete(self._key(run, call))

    async def remember(self, ws: WorkspaceRef, sandbox_uuid: UUID) -> UUID | None:
        """记下工作区这次用的沙箱，返回上一次用的（没有就 None）。"""
        key = f"{self._prefix}:seen:{ws.root_thread_uuid}"
        previous = await self._r.getset(key, str(sandbox_uuid))
        await self._r.expire(key, self._TTL_S)
        if isinstance(previous, bytes):
            previous = previous.decode()
        return UUID(previous) if previous else None


@dataclass(frozen=True)
class PollingPolicy:
    """跟随时多久问一次：有输出时快，没输出时逐步放慢到上限；断线后指数退避重连，次数用完就放弃。"""

    min_s: float = 0.3
    max_s: float = 2.0
    #: 断线后重新接上的尝试次数（指数退避，封顶 10 秒；合计约两分钟）
    reconnects: int = 15

    def next_poll(self, current: float, had_output: bool) -> float:
        return self.min_s if had_output else min(current * 2, self.max_s)

    def reconnect_delay(self, attempt: int) -> float | None:
        if attempt > self.reconnects:
            return None
        return float(min(2 ** (attempt - 1), 10))


class StallDetector:
    """卡死判定：连续 idle_s 没有新输出，且这段时间沙箱的 CPU 一直很低（至少两次采样）。

    ★ CPU 口径不是沙箱自己的（整节点）就不下结论：会被别的 Pod 的负载带偏，宁可不判也不误杀。
    """

    #: 卡死候选期间多久取一次 CPU
    SAMPLE_S = 15.0

    def __init__(self, idle_s: float | None, cpu_pct: float) -> None:
        self._idle_s = idle_s
        self._cpu_pct = cpu_pct
        self._quiet_since = time.monotonic()
        self._samples: list[float] = []
        self._next_sample = 0.0

    def observe(self, had_output: bool, now: float) -> None:
        if had_output:
            self._quiet_since, self._samples = now, []

    def quiet_for(self, now: float) -> float:
        return now - self._quiet_since

    def due(self, now: float) -> bool:
        if not self._idle_s or self.quiet_for(now) < self._idle_s or now < self._next_sample:
            return False
        self._next_sample = now + self.SAMPLE_S
        return True

    def judge(self, usage: ResourceUsage | None, now: float) -> bool:
        if usage is None or not usage.per_sandbox():
            return False
        self._samples.append(usage.cpu_pct)
        if usage.cpu_pct >= self._cpu_pct:
            self._samples.clear()
            return False
        # 一次低可能只是恰好在等 IO：至少两次采样（间隔 SAMPLE_S）都很低才判
        return (
            len(self._samples) >= 2 and self.quiet_for(now) >= (self._idle_s or 0) + self.SAMPLE_S
        )


@dataclass
class OutputBuffer:
    """边读边只留头尾：命令输出多大都不会撑爆内存；交出时注明中间省略了多少字节。"""

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
class CommandExecution:
    """一次命令执行：在哪个沙箱、命令 id、读到哪了、附加说明。输出只经它追加。"""

    sandbox_uuid: UUID
    cmd_id: str
    out: OutputBuffer
    cursor: int = 0
    notes: list[str] = field(default_factory=list)

    def advance(self, chunk: LogChunk) -> bool:
        self.out.add(chunk.data)
        self.cursor = chunk.cursor
        return bool(chunk.data)

    def result(self, exit_code: int | None, notice: str = "") -> ExecuteResponse:
        text, truncated = self.out.text()
        if self.notes:
            text += "\n" + "\n".join(f"[{n}]" for n in self.notes)
        return ExecuteResponse(output=notice + text, exit_code=exit_code, truncated=truncated)


class _Gone(Exception):
    """命令所在的沙箱已经不是原来那个了（换了，或重启过）。"""


class ShellSandbox:
    """一个 run 一个实例；同一工作区的多个实例共用一个沙箱。

    existing_only：只用已经在跑的沙箱、不新建（评测原地执行）。
    """

    def __init__(
        self,
        *,
        native: NativeSandboxService,
        exec: SandboxExec,
        ledger: ExecutionLedger,
        ws: WorkspaceRef,
        run_id: str,
        spec: SandboxSpec,
        polling: PollingPolicy | None = None,
        output_limit: int = 100_000,
        idle_s: float | None = None,
        stall_cpu_pct: float = 2.0,
        existing_only: bool = False,
    ) -> None:
        self._native = native
        self._exec = exec
        self._ledger = ledger
        self._ws = ws
        self._run = run_id
        self._spec = spec
        self._poll = polling or PollingPolicy()
        self._limit = output_limit
        self._idle_s = idle_s
        self._stall_pct = stall_cpu_pct
        self._existing_only = existing_only

    @property
    def id(self) -> str:
        return f"sbx:{self._ws.root_thread_uuid}"

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
        with tracer().start_as_current_span("sandbox.exec") as span:
            span.set_attribute("atlas.sandbox.workspace", str(self._ws.root_thread_uuid))
            started = time.monotonic()
            ref = await self._ensure()
            notice = await self._notice(ref)
            submitted = await self._ledger.get(self._run, exec_id) if exec_id else None
            if submitted is None:
                ex = await self._submit(ref, command, cwd or WORKDIR, timeout)
                if exec_id:
                    await self._ledger.put(self._run, exec_id, ex.sandbox_uuid, ex.cmd_id)
            else:
                logger.info("%s:%s 已提交过（%s）→ 接上，不重跑", self._run, exec_id, submitted[1])
                ex = CommandExecution(submitted[0], submitted[1], OutputBuffer(self._limit))
            span.set_attribute("atlas.sandbox.uuid", str(ex.sandbox_uuid))
            span.set_attribute("atlas.sandbox.cmd_id", ex.cmd_id)
            try:  # ★ 拿到命令 id 之后的每个 await 都可能被取消：都要在这个 try 里
                if ex.sandbox_uuid != ref.sandbox_uuid:
                    raise _Gone(ex.cmd_id)  # 重放时沙箱已经换过
                exit_code = await self._follow(ex)
            except _Gone as exc:
                raise SandboxUnavailable(_GONE) from exc
            except asyncio.CancelledError:
                # ★ 不从账本里划掉：重放同一个调用读到的是它中断时的输出，不会再跑一遍
                self._interrupt_later(ex.sandbox_uuid, ex.cmd_id)
                raise
            span.set_attribute("atlas.sandbox.exit_code", -1 if exit_code is None else exit_code)
            span.set_attribute(
                "atlas.sandbox.duration_ms", int((time.monotonic() - started) * 1000)
            )
            span.set_attribute("atlas.sandbox.output_bytes", ex.out.total)
        return ex.result(exit_code, notice)

    # ------------------------------------------------------------------ 内部

    async def _ensure(self) -> SandboxRef:
        with tracer().start_as_current_span("sandbox.ensure"):
            if self._existing_only:
                ref = await self._native.current(self._ws)
                if ref is None:
                    raise SandboxUnavailable("工作区没有正在运行的沙箱")
                return ref
            try:
                return await self._native.ensure(self._ws, self._spec)
            except SandboxUnavailable:
                raise
            except Exception as exc:  # 配额、镜像准入、建不起来、没就绪……一律变成工具错误
                raise SandboxUnavailable(f"沙箱启动失败：{exc}") from exc

    async def _notice(self, ref: SandboxRef) -> str:
        previous = await self._ledger.remember(self._ws, ref.sandbox_uuid)
        return REBUILT if previous is not None and previous != ref.sandbox_uuid else ""

    async def _submit(
        self, ref: SandboxRef, command: str, cwd: str, timeout: int | None
    ) -> CommandExecution:
        start = asyncio.ensure_future(
            self._exec.start(ref.sandbox_uuid, CommandSpec(command, cwd=cwd, timeout_s=timeout))
        )
        try:
            cmd_id = await asyncio.shield(start)
        except asyncio.CancelledError:
            # 取消落在提交途中：请求可能已经到了 execd。等它拿到 id 再中断，免得命令在沙箱里没人管
            start.add_done_callback(lambda t: self._interrupt_submitted(ref.sandbox_uuid, t))
            raise
        except SubmitUnknown as exc:
            raise SandboxUnavailable(
                "提交命令时连接中断：命令可能已经开始执行，结果未知；"
                "可以先检查 /workspace 的状态再决定是否重试"
            ) from exc
        except (SandboxGone, ExecdUnreachable, ExecdRejected) as exc:
            raise SandboxUnavailable(f"沙箱拒绝了命令：{exc}") from exc
        return CommandExecution(ref.sandbox_uuid, cmd_id, OutputBuffer(self._limit))

    async def _follow(self, ex: CommandExecution) -> int | None:
        delay, attempts = self._poll.min_s, 0
        stall = StallDetector(self._idle_s, self._stall_pct)
        sid = ex.sandbox_uuid
        while True:
            try:
                had_output = ex.advance(await self._exec.read(sid, ex.cmd_id, ex.cursor))
                status = await self._exec.status(sid, ex.cmd_id)
                if not status.running:
                    ex.advance(await self._exec.read(sid, ex.cmd_id, ex.cursor))  # 收尾
                    if status.failed_to_start():
                        ex.notes.append(f"命令没能启动：{status.error}")
                    return status.exit_code
                attempts = 0
                now = time.monotonic()
                stall.observe(had_output, now)
                delay = self._poll.next_poll(delay, had_output)
                if stall.due(now) and stall.judge(await self._exec.usage(sid), now):
                    ex.notes.append(
                        f"命令连续 {int(stall.quiet_for(now))} 秒既无输出、也无 CPU 活动，"
                        "判定为卡死，已结束"
                    )
                    await self._exec.interrupt(sid, ex.cmd_id)
                await asyncio.sleep(delay)
            except (CommandGone, SandboxGone) as exc:
                raise _Gone(ex.cmd_id) from exc
            except ExecdUnreachable as exc:
                attempts += 1
                wait = self._poll.reconnect_delay(attempts)
                logger.info("命令 %s 的连接中断（第 %d 次）：%s", ex.cmd_id, attempts, exc)
                if wait is None:
                    raise SandboxUnavailable("与沙箱的连接中断且无法恢复，命令结果未知") from exc
                await asyncio.sleep(wait)  # 下一次按同一个 sandbox_uuid 重新解析地址

    def _interrupt_submitted(self, sandbox_uuid: UUID, start: asyncio.Future[str]) -> None:
        if start.cancelled() or start.exception() is not None:
            return  # 没提交成功：没有要中断的
        self._interrupt_later(sandbox_uuid, start.result())

    def _interrupt_later(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        """这一轮被取消：结束沙箱里的命令（不等它回来）。"""

        async def interrupt() -> None:
            try:
                await self._exec.interrupt(sandbox_uuid, cmd_id)
            except Exception:
                logger.warning("取消沙箱命令 %s 失败", cmd_id, exc_info=True)

        task = asyncio.get_running_loop().create_task(interrupt())
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)


async def exec_in_running(
    native: NativeSandboxService,
    exec: SandboxExec,
    ledger: ExecutionLedger,
    ws: WorkspaceRef,
    *,
    command: str,
    profile: SandboxProfile,
    cwd: str = WORKDIR,
    timeout: int | None = None,
    files: Sequence[tuple[str, bytes]] = (),
    output_limit: int = 100_000,
    polling: PollingPolicy | None = None,
) -> ExecuteResponse:
    """在工作区**已经在跑**的沙箱里执行一条命令，等它结束（评测在 Agent 的现场里校验）。

    ★ 不新建沙箱：要看的是 Agent 留下的现场，新建一个空沙箱去执行，结论就是错的。
    ★ 只认 profile 这一档（调用方给评测档）：这条路径不经过审批模式。
    files：执行前先写进沙箱的 (绝对路径, 内容)，父目录要已经存在。
    """
    ref = await native.current(ws)
    actual = await native.profile_of(ref) if ref is not None else None
    if ref is None or actual is None:
        raise SandboxNotRunning(
            "会话的工作区没有正在运行的沙箱（Agent 没有执行过命令，或已闲置回收）"
        )
    if actual != profile:
        raise SandboxProfileDenied(f"这个沙箱是 {actual} 档，不能直接执行命令")
    if files:
        try:
            session = await exec.open_files(ref.sandbox_uuid)
        except SandboxGone as exc:
            raise SandboxNotRunning("沙箱已经不在了") from exc
        try:
            for path, data in files:
                await asyncio.to_thread(session.upload, path, data)
        except Exception as exc:
            raise SandboxUnavailable(f"往沙箱里写文件失败：{exc}") from exc
        finally:
            session.close()
    sandbox = ShellSandbox(
        native=native,
        exec=exec,
        ledger=ledger,
        ws=ws,
        run_id=f"exec-{uuid4().hex}",
        spec=SandboxSpec(profile=actual),
        polling=polling,
        output_limit=output_limit,
        existing_only=True,
    )
    return await sandbox.aexecute(command, timeout=timeout, cwd=cwd)
