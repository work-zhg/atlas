"""AgentProcess：把 agent 子进程包装成一条按行收发的 ``MessageChannel``（代码设计 §7.3）。

ACP 的 stdio 传输：一条消息一行，stdout 上只有协议消息，日志走 stderr（Bridge 设计 §2.3）。

实现要点：
  · **读缓冲上限显式设置**。asyncio 默认只有 64 KiB，而一条 update 可能带着整个文件内容。
  · **超长行只丢这一行**。asyncio 的 readline 超限时会清空整个缓冲区，连带丢掉后面
    完整的行（例如紧跟着的权限请求）。这里改用 readuntil，超限后逐块跳到下一个换行。
  · **stderr 持续读取**。不读的话管道写满会阻塞 agent。
  · **独立进程组，终止时整组终止**。CLI 会拉起自己的子进程（shell 命令、stdio 类 MCP
    server），只终止适配器进程会让它们成为孤儿，继续在 Pod 里运行。
  · **环境变量显式给出**，不继承 bridge 的环境（Bridge 设计 §8.3）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from atlas_jsonrpc import ChannelClosed

__all__ = ["AgentLaunch", "AgentProcess", "OversizeLine"]

logger = logging.getLogger(__name__)

_NEWLINE = b"\n"
_PREVIEW_BYTES = 512


@dataclass(frozen=True, slots=True)
class AgentLaunch:
    argv: tuple[str, ...]
    cwd: Path
    #: 传给 agent 的全部环境变量（白名单构造，不继承 bridge 的环境）
    env: Mapping[str, str] = field(default_factory=dict)
    #: 以哪个系统用户 / 组运行 agent。None = 不切换（本地开发；生产必须设置，§8.3）
    user: int | str | None = None
    group: int | str | None = None
    #: 内存不足时让内核先杀 agent（§8.6）。None = 不设置；非 Linux 上自动跳过
    oom_score_adj: int | None = 900
    max_line_bytes: int = 32 * 1024 * 1024
    stderr_line_bytes: int = 2048


@dataclass(frozen=True, slots=True)
class OversizeLine:
    """一行超过上限、被跳过的输出。``head`` 是开头若干字节，用来判断它是什么。"""

    size: int
    head: bytes


class AgentProcess:
    """一个 agent 子进程。实现 ``MessageChannel``：每条消息是 stdout 上的一行。"""

    def __init__(
        self,
        launch: AgentLaunch,
        *,
        on_oversize: Callable[[OversizeLine], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        self._launch = launch
        self._on_oversize = on_oversize
        self._on_stderr = on_stderr or (lambda line: logger.info("agent stderr: %s", line))
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 生命周期

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    async def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("agent 进程已经启动过")
        launch = self._launch
        self._proc = await asyncio.create_subprocess_exec(
            *launch.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=launch.cwd,
            env=dict(launch.env),
            limit=launch.max_line_bytes,
            start_new_session=True,  # 独立进程组：终止时连同它的子进程一起终止
            user=launch.user,
            group=launch.group,
        )
        self._set_oom_score_adj()
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="agent-stderr")

    async def wait(self) -> int:
        """等进程退出，返回退出码。"""
        proc = self._require()
        code = await proc.wait()
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        return code

    async def terminate(self, grace: float) -> int:
        """SIGTERM 整个进程组，``grace`` 秒后仍未退出则 SIGKILL（Bridge 设计 §6.1）。"""
        proc = self._require()
        if proc.returncode is None:
            self._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), grace)
            except TimeoutError:
                logger.warning("agent 在 %gs 内未响应 SIGTERM，强制终止", grace)
                self._signal_group(signal.SIGKILL)
        # 进程已退出，进程组里可能还有它留下的子进程
        self._signal_group(signal.SIGKILL)
        return await self.wait()

    # ------------------------------------------------------------------ MessageChannel

    async def send(self, text: str) -> None:
        proc = self._require()
        if proc.stdin is None or proc.returncode is not None:
            raise ChannelClosed("agent 进程已退出")
        async with self._write_lock:
            try:
                proc.stdin.write(text.encode() + _NEWLINE)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ChannelClosed("agent 的 stdin 已关闭") from exc

    async def receive(self) -> str | None:
        """下一行输出；进程关闭 stdout 后返回 None。空行跳过，超长行跳过并报告。"""
        proc = self._require()
        assert proc.stdout is not None
        while True:
            line = await self._read_line(proc.stdout, self._report_stdout_oversize)
            if line is None:
                return None
            text = line.rstrip(b"\r\n")
            if text:
                return text.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ 内部

    def _require(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise RuntimeError("agent 进程尚未启动")
        return self._proc

    async def _read_line(
        self, stream: asyncio.StreamReader, on_oversize: Callable[[OversizeLine], None]
    ) -> bytes | None:
        while True:
            try:
                return await stream.readuntil(_NEWLINE)
            except asyncio.IncompleteReadError as exc:
                # 到达 EOF：最后一行可能没有换行
                return exc.partial or None
            except asyncio.LimitOverrunError as exc:
                on_oversize(await self._skip_oversize(stream, exc.consumed))
                # 继续读下一行

    def _report_stdout_oversize(self, oversize: OversizeLine) -> None:
        if self._on_oversize is not None:
            self._on_oversize(oversize)
        else:
            logger.warning("agent 输出了一行 %d 字节的消息，超过上限，已跳过", oversize.size)

    @staticmethod
    def _report_stderr_oversize(oversize: OversizeLine) -> None:
        logger.warning("agent 的 stderr 输出了一行 %d 字节的日志，已跳过", oversize.size)

    async def _skip_oversize(self, stream: asyncio.StreamReader, consumed: int) -> OversizeLine:
        """跳过当前这一行（直到并包括下一个换行），只保留开头作为预览。

        ★ LimitOverrunError 之后数据仍留在缓冲区里（readuntil 的约定），所以可以逐块读走。
        """
        chunk = await stream.readexactly(consumed)
        head, size = chunk[:_PREVIEW_BYTES], len(chunk)
        while True:
            try:
                rest = await stream.readuntil(_NEWLINE)
                return OversizeLine(size + len(rest), head)
            except asyncio.LimitOverrunError as exc:
                size += len(await stream.readexactly(exc.consumed))
            except asyncio.IncompleteReadError as exc:
                return OversizeLine(size + len(exc.partial), head)

    async def _drain_stderr(self) -> None:
        proc = self._require()
        assert proc.stderr is not None
        limit = self._launch.stderr_line_bytes
        while True:
            line = await self._read_line(proc.stderr, self._report_stderr_oversize)
            if line is None:
                return
            text = line.rstrip(b"\r\n")[:limit].decode("utf-8", errors="replace")
            if text:
                self._on_stderr(text)

    def _signal_group(self, sig: signal.Signals) -> None:
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)

    def _set_oom_score_adj(self) -> None:
        adj = self._launch.oom_score_adj
        if adj is None or self._proc is None:
            return
        path = Path(f"/proc/{self._proc.pid}/oom_score_adj")
        try:
            path.write_text(str(adj))
        except FileNotFoundError:
            pass  # 非 Linux
        except OSError:
            logger.warning("无法设置 agent 的 oom_score_adj", exc_info=True)
