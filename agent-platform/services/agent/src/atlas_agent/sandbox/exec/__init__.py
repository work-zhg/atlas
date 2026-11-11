"""沙箱 · 命令执行（c4-model/agent/sandbox/code-sandbox-exec.html）：在工作区的沙箱里后台执行命令。

    ShellSandbox      实现引擎的 SandboxProtocol：确保沙箱 → 后台提交 → 跟随到退出；重放不重跑、断线
                      接上、卡死和用户取消时中断；失败一律抛 SandboxUnavailable
    exec_in_running   评测原地校验：只在已经在跑的评测档沙箱里执行，不新建
    ExecutionLedger   (run, 工具调用) → (sandbox_uuid, 命令 id)：库里，先记「提交中」再提交
    PollingPolicy · StallDetector · CommandExecution · OutputBuffer

★ 只拿 sandbox_uuid：地址和令牌都在基座里。沙箱换了（SandboxGone）或重启过（CommandGone）如实报错、
  不重跑；连接断了用同一个 sandbox_uuid 接着读（基座重新解析地址）。
★ 活动不在这里记：基座执行在提交、命令结束、打开文件时自动记，命令执行不用回头通知原生沙箱。
  只有跟随中定期确认一次「还在跑」（confirm_running），Redis 丢过数据也补得回来。
★ 中断沙箱里的命令只在用户取消时发生（看 run 的取消位）：交接、租约丢失、停机也会取消这个任务，
  那时只停止跟随，命令留在沙箱里接着跑，接手的实例凭执行账本接上。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

import httpx
from atlas_engine.contracts import ExecuteResponse, SandboxUnavailable
from atlas_engine.contracts.spec import SandboxProfile, SandboxSpec
from sqlalchemy import delete, update

from ...db.models import SandboxExecution
from ...db.session import fetch_one
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
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "CancelSignal",
    "CommandExecution",
    "ExecutionLedger",
    "OutputBuffer",
    "PollingPolicy",
    "SandboxNotRunning",
    "SandboxProfileDenied",
    "ShellSandbox",
    "StallDetector",
    "Submission",
    "exec_in_running",
]

logger = logging.getLogger(__name__)

#: 取消时发出的中断请求（持有引用，防止任务被回收）
_BACKGROUND: set[asyncio.Task[Any]] = set()
#: 沙箱换了的提示：/data 下的文件（工作区、技能）还在，别的都没了
REBUILT = (
    "[沙箱环境已重建：/data 下的文件（工作区、技能）仍在；"
    "安装的软件包、HOME 与 /repos 里的内容、后台进程已丢失]\n"
)
_GONE = (
    "命令执行期间沙箱重启（可能内存不足或被回收），命令已中断、结果丢失；"
    "工作区里已经写入的文件仍在"
)
_SUBMIT_UNKNOWN = (
    "提交命令时连接中断：命令可能已经开始执行，结果未知；可以先检查工作区的状态再决定是否重试"
)
#: 跟随中多久确认一次「命令还在跑」（活动账本）
_CONFIRM_RUNNING_S = 60.0
#: 被取消时查取消位最多等多久：查不到就当没置位（不中断）
_CANCEL_CHECK_S = 2.0


class SandboxNotRunning(RuntimeError):
    """工作区没有正在运行的沙箱。直接执行不新建沙箱。"""


class SandboxProfileDenied(RuntimeError):
    """沙箱的档位不允许直接执行。"""


class CancelSignal(Protocol):
    """run 的取消位（注入 RedisCancelToken）：用户点了「停止」才置位，交接、租约丢失不置位。"""

    async def is_cancelled(self) -> bool: ...


@dataclass(frozen=True)
class Submission:
    """执行账本里的一次调用：在哪个沙箱；cmd_id 为空 = 提交中（请求可能已经到了 execd）。"""

    sandbox_uuid: UUID
    cmd_id: str | None


class ExecutionLedger:
    """(run, tool_call) → (sandbox_uuid, cmd_id)：同一个调用被重放时接上原来的命令，而不是再跑一遍
    —— 接管的实例重放也接得上。

    ★ 调用记录在库里（sandbox_execution）：提交前先记「提交中」，拿到命令 id 再补上。实例死在两者
      之间，重放看到「提交中」就不再提交（execd 的提交没有幂等键，没法让它自己去重）。
    ★ 另记每个工作区上次执行用的沙箱（Redis，24 小时过期），用来提示环境已重建；丢了只是少一次提示。
    """

    _SEEN_TTL_S = 24 * 3600

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        *,
        prefix: str = "atlas:exec",
    ) -> None:
        self._sessions = sessions
        self._r = redis
        self._prefix = prefix

    async def get(self, run: UUID, call: str) -> Submission | None:
        async with self._sessions() as session:
            row = await fetch_one(
                session,
                SandboxExecution,
                SandboxExecution.run_uuid == run,
                SandboxExecution.tool_call_id == call,
            )
        return None if row is None else Submission(row.sandbox_uuid, row.cmd_id)

    async def intend(self, run: UUID, call: str, sandbox_uuid: UUID) -> None:
        """要在这个沙箱里提交了：记「提交中」。"""
        async with self._sessions() as session:
            session.add(
                SandboxExecution(run_uuid=run, tool_call_id=call, sandbox_uuid=sandbox_uuid)
            )
            await session.commit()

    async def put(self, run: UUID, call: str, sandbox_uuid: UUID, cmd_id: str) -> None:
        """提交上了：补上命令 id。"""
        async with self._sessions() as session:
            await session.execute(
                update(SandboxExecution)
                .where(SandboxExecution.run_uuid == run, SandboxExecution.tool_call_id == call)
                .values(sandbox_uuid=sandbox_uuid, cmd_id=cmd_id)
            )
            await session.commit()

    async def drop(self, run: UUID, call: str) -> None:
        """确定没提交上（沙箱拒绝、连不上）：去掉「提交中」，重放时照常提交。"""
        async with self._sessions() as session:
            await session.execute(
                delete(SandboxExecution).where(
                    SandboxExecution.run_uuid == run, SandboxExecution.tool_call_id == call
                )
            )
            await session.commit()

    async def remember(self, ws: WorkspaceRef, sandbox_uuid: UUID) -> UUID | None:
        """记下工作区这次用的沙箱，返回上一次用的（没有就 None）。"""
        key = f"{self._prefix}:seen:{ws.root_thread_uuid}"
        previous = await self._r.getset(key, str(sandbox_uuid))
        await self._r.expire(key, self._SEEN_TTL_S)
        if isinstance(previous, bytes):
            previous = previous.decode()
        return UUID(previous) if previous else None


@dataclass(frozen=True)
class PollingPolicy:
    """跟随时多久问一次：有输出时快，没输出时逐步放慢到上限；断线后指数退避重连，有封顶。

    ★ 重连不设次数上限：沙箱还在就一直按同一个游标接着读，确认沙箱没了（SandboxGone）才报。次数上限
      就是一个到点放弃的超时 —— 放弃时命令其实还在跑，模型拿到「结果未知」常常原样重跑。
      用户等不及可以取消。
    """

    min_s: float = 0.3
    max_s: float = 2.0
    reconnect_max_s: float = 30.0

    def next_poll(self, current: float, had_output: bool) -> float:
        return self.min_s if had_output else min(current * 2, self.max_s)

    def reconnect_delay(self, attempt: int) -> float:
        return float(min(2 ** (attempt - 1), self.reconnect_max_s))


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
        return ExecuteResponse(
            output=notice + text, exit_code=exit_code, truncated=truncated, rebuilt=bool(notice)
        )


class _Gone(Exception):
    """命令所在的沙箱已经不是原来那个了（换了，或重启过）。"""


class ShellSandbox:
    """一个 run 一个实例；同一工作区的多个实例共用一个沙箱。

    cancel：run 的取消位，被取消时用它分辨「用户要停」和「这个实例不跟了」。不给（评测原地执行，
        没有交接这回事）就把每次取消都当用户要停。
    existing_only：只用已经在跑的沙箱、不新建（评测原地执行）。
    """

    def __init__(
        self,
        *,
        native: NativeSandboxService,
        exec: SandboxExec,
        ledger: ExecutionLedger,
        ws: WorkspaceRef,
        run_id: UUID,
        spec: SandboxSpec,
        cancel: CancelSignal | None = None,
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
        self._cancel = cancel
        self._poll = polling or PollingPolicy()
        self._limit = output_limit
        self._idle_s = idle_s
        self._stall_pct = stall_cpu_pct
        self._existing_only = existing_only
        #: 工作区目录已经确认存在的沙箱（换了沙箱要再建一次）
        self._workdir_ready: UUID | None = None

    @property
    def id(self) -> str:
        return f"sbx:{self._ws.root_thread_uuid}"

    @property
    def workdir(self) -> str:
        """命令默认的 cwd：本会话所用工作区在沙箱里的路径（沙箱是这个人共用的，cwd 按工作区分）。"""
        return self._ws.layout().workdir

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
                if cwd is None:
                    await self._prepare_workdir(ref)
                ex = await self._submit(ref, command, cwd or self.workdir, timeout, exec_id)
            elif submitted.cmd_id is None:
                # 上一个执行者记了「提交中」就没了：命令可能已经开跑，不能再提交一次
                raise SandboxUnavailable(_SUBMIT_UNKNOWN, kind="result_unknown")
            else:
                logger.info(
                    "%s:%s 已提交过（%s）→ 接上，不重跑", self._run, exec_id, submitted.cmd_id
                )
                ex = CommandExecution(
                    submitted.sandbox_uuid, submitted.cmd_id, OutputBuffer(self._limit)
                )
            span.set_attribute("atlas.sandbox.uuid", str(ex.sandbox_uuid))
            span.set_attribute("atlas.sandbox.cmd_id", ex.cmd_id)
            pending = exec_id if submitted is None else None  # 还没补上命令 id 的调用
            try:  # ★ 拿到命令 id 之后的每个 await 都可能被取消：都要在这个 try 里
                if pending:
                    await self._record(pending, ex)
                    pending = None
                if ex.sandbox_uuid != ref.sandbox_uuid:
                    raise _Gone(ex.cmd_id)  # 重放时沙箱已经换过
                exit_code = await self._follow(ex)
            except _Gone as exc:
                raise SandboxUnavailable(_GONE, kind="lost") from exc
            except asyncio.CancelledError:
                if pending:  # 取消落在补记上：后台补完，接手的实例凭它接上
                    _in_background(self._record(pending, ex))
                # ★ 只有用户取消才中断；交接、租约丢失时命令留在沙箱里，接手的实例凭账本接上。
                #   账本都不划掉：重放同一个调用读到的是它中断时的输出，不会再跑一遍
                if await self._user_cancelled():
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

    async def _prepare_workdir(self, ref: SandboxRef) -> None:
        """工作区目录在沙箱里可能还不存在：沙箱是这个人共用的，挂的是整个 {user}/，新会话还没写过
        文件时对象存储里没有这个前缀。execd 不接受不存在的 cwd，先建好。每个沙箱只建一次。"""
        if self._workdir_ready == ref.sandbox_uuid:
            return
        try:
            files = await self._exec.open_files(ref.sandbox_uuid)
        except (SandboxGone, ExecdUnreachable, ExecdRejected) as exc:
            raise SandboxUnavailable(f"沙箱拒绝了命令：{exc}") from exc
        try:
            await asyncio.to_thread(files.mkdir, self.workdir)
        except httpx.HTTPError as exc:
            raise SandboxUnavailable(f"在沙箱里建工作区目录失败：{exc}") from exc
        finally:
            files.close()
        self._workdir_ready = ref.sandbox_uuid

    async def _notice(self, ref: SandboxRef) -> str:
        previous = await self._ledger.remember(self._ws, ref.sandbox_uuid)
        return REBUILT if previous is not None and previous != ref.sandbox_uuid else ""

    async def _submit(
        self, ref: SandboxRef, command: str, cwd: str, timeout: int | None, exec_id: str | None
    ) -> CommandExecution:
        if exec_id:
            await self._ledger.intend(self._run, exec_id, ref.sandbox_uuid)  # ★ 先记后提交
        start = asyncio.ensure_future(
            self._exec.start(ref.sandbox_uuid, CommandSpec(command, cwd=cwd, timeout_s=timeout))
        )
        try:
            cmd_id = await asyncio.shield(start)
        except asyncio.CancelledError:
            # 取消落在提交途中：请求可能已经到了 execd。等它拿到 id 再补记账本（接手的实例凭它
            # 接上），用户取消的再中断它，免得命令在沙箱里没人管
            stop = False
            try:
                stop = await self._user_cancelled()
            finally:  # 查取消位时又被取消也要挂上：拿到 id 之后总得有人记账
                start.add_done_callback(
                    lambda t: self._settle_cancelled_submit(ref.sandbox_uuid, exec_id, t, stop=stop)
                )
            raise
        except SubmitUnknown as exc:
            raise SandboxUnavailable(_SUBMIT_UNKNOWN, kind="result_unknown") from exc
        except (SandboxGone, ExecdUnreachable, ExecdRejected) as exc:
            if exec_id:  # 确定没提交上：去掉「提交中」，重放时照常提交
                try:
                    await self._ledger.drop(self._run, exec_id)
                except Exception:
                    logger.warning("去掉调用 %s 的「提交中」失败", exec_id, exc_info=True)
            raise SandboxUnavailable(f"沙箱拒绝了命令：{exc}") from exc
        return CommandExecution(ref.sandbox_uuid, cmd_id, OutputBuffer(self._limit))

    async def _follow(self, ex: CommandExecution) -> int | None:
        delay, attempts = self._poll.min_s, 0
        stall = StallDetector(self._idle_s, self._stall_pct)
        sid = ex.sandbox_uuid
        next_confirm = 0.0
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
                if now >= next_confirm:  # 活动账本丢过数据也补得回来：闲置回收不会删掉在跑的命令
                    await self._exec.confirm_running(sid, ex.cmd_id)
                    next_confirm = now + _CONFIRM_RUNNING_S
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
                logger.info("命令 %s 的连接中断（第 %d 次）：%s", ex.cmd_id, attempts, exc)
                # 不设次数上限：下一次按同一个 sandbox_uuid 重新解析地址，沙箱没了会抛 SandboxGone
                await asyncio.sleep(self._poll.reconnect_delay(attempts))

    async def _record(self, exec_id: str, ex: CommandExecution) -> None:
        """补上命令 id。写不进去也接着跟随：账本停在「提交中」，重放时报结果未知，不会重跑。"""
        try:
            await self._ledger.put(self._run, exec_id, ex.sandbox_uuid, ex.cmd_id)
        except Exception:
            logger.warning("记命令 %s 的执行账本失败", ex.cmd_id, exc_info=True)

    async def _user_cancelled(self) -> bool:
        """这次取消是不是用户要停（取消位置位）。

        ★ 查不到（Redis 不可用、超时）当没置位：宁可让命令多跑，也不误杀。这时取消本身也送不到
          runner（它查的是同一个取消位），行为一致。
        """
        if self._cancel is None:
            return True
        try:
            async with asyncio.timeout(_CANCEL_CHECK_S):
                return await self._cancel.is_cancelled()
        except Exception:
            logger.warning(
                "run %s 查取消位失败，按未取消处理（不中断命令）", self._run, exc_info=True
            )
            return False

    def _settle_cancelled_submit(
        self, sandbox_uuid: UUID, exec_id: str | None, start: asyncio.Future[str], *, stop: bool
    ) -> None:
        if start.cancelled() or start.exception() is not None:
            return  # 没拿到 id：账本里留着「提交中」，重放时如实报结果未知
        cmd_id = start.result()

        async def settle() -> None:
            if exec_id:
                try:
                    await self._ledger.put(self._run, exec_id, sandbox_uuid, cmd_id)
                except Exception:
                    logger.warning("补记命令 %s 的执行账本失败", cmd_id, exc_info=True)
            if stop:
                await self._interrupt(sandbox_uuid, cmd_id)

        _in_background(settle())

    def _interrupt_later(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        """用户取消了这一轮：结束沙箱里的命令（不等它回来）。"""
        _in_background(self._interrupt(sandbox_uuid, cmd_id))

    async def _interrupt(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        try:
            await self._exec.interrupt(sandbox_uuid, cmd_id)
        except Exception:
            logger.warning("取消沙箱命令 %s 失败", cmd_id, exc_info=True)


def _in_background(work: Any) -> None:
    task = asyncio.get_running_loop().create_task(work)
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
    cwd: str | None = None,
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
        run_id=uuid4(),
        spec=SandboxSpec(profile=actual),
        polling=polling,
        output_limit=output_limit,
        existing_only=True,
    )
    return await sandbox.aexecute(command, timeout=timeout, cwd=cwd)
