"""server 侧：PodSandbox —— SandboxProtocol 的 OpenSandbox 实现（设计 §8）。

    首次 execute  经 Ensurer 确保工作区的沙箱在跑（按需建；子智能体与父会话共用，键是工作区）
    提交          execd 后台模式 POST /command → init 事件里的命令 id；
                  记进进程内存 {run}:{tool_call} → id
    跟随          轮询 GET logs?cursor= 与 GET status，直到退出；不设总时长上限
    断线 / 换 IP  重新 ensure，用同一个命令 id 和游标接上 —— 命令属于 execd，不属于连接
    沙箱换了      新沙箱的 execd 不认识这个 id（404）→ 工具错误「执行期间沙箱重启」，不重跑
    取消          DELETE /command?id=（execd 杀整个进程组）
    沙箱 id 变了  在工具结果前提示「环境已重建」

★ 输出的头尾截断在这里做（execd 不截断）；卡死判定（无输出且容器 CPU≈0）demo 没做，见 README §9。
★ 失败一律 SandboxUnavailable：中间件把它转成工具错误，不让整轮失败。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from agent import ExecuteResponse, SandboxUnavailable
from opensandbox_client import ExecdClient
from provision import EXECD_PORT, SBX, Endpoint, Ensurer, ImageDenied, QuotaExceeded, sbx_spec
from tracing import say

FOREGROUND = False  # 实验 1：改成 True，用前台模式跑命令
POLL_MIN_S, POLL_MAX_S = 0.05, 0.3  # 真实 0.3 / 2.0
OUTPUT_LIMIT = 4000  # 回给模型的输出：头尾各一半（真实 100 KB）
RECONNECT_ATTEMPTS = 5
DEFAULT_IMAGE = "atlas-sandbox:dev"


class _Gone(Exception):
    """execd 不认识这个命令 id：沙箱在命令执行期间被换掉了。"""


@dataclass
class Output:
    """边读边保留头尾：命令输出多大都不打爆内存。"""

    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total: int = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = OUTPUT_LIMIT // 2 - len(self.head)
        self.head += chunk[:room]
        self.tail = (self.tail + chunk[max(room, 0) :])[-OUTPUT_LIMIT // 2 :]

    def text(self) -> tuple[str, bool]:
        kept = len(self.head) + len(self.tail)
        if self.total <= kept:
            return (bytes(self.head) + bytes(self.tail)).decode(errors="replace"), False
        gap = f"\n…（省略中间 {self.total - kept} 字节）…\n"
        return bytes(self.head).decode(errors="replace") + gap + bytes(self.tail).decode(
            errors="replace"
        ), True


@dataclass
class ExecRecord:
    sandbox_id: str
    cmd_id: str
    cursor: int = 0
    out: Output = field(default_factory=Output)


@dataclass
class ProcessMemory:
    """一个 server 进程的内存（不共享）：命令映射、每个工作区上次见到的沙箱。"""

    execs: dict[str, ExecRecord] = field(default_factory=dict)
    seen: dict[str, str] = field(default_factory=dict)


class PodSandbox:
    """一个 run 一个实例；同一工作区的多个实例指向同一个沙箱。"""

    def __init__(
        self,
        *,
        ensurer: Ensurer,
        workspace_thread_id: str,
        user_id: str,
        run_id: str,
        memory: ProcessMemory,
        transport: httpx.AsyncBaseTransport,
    ) -> None:
        self._ensurer, self._ws, self._user, self._run = (
            ensurer,
            workspace_thread_id,
            user_id,
            run_id,
        )
        self._mem, self._transport = memory, transport

    @property
    def id(self) -> str:
        return f"sbx:{self._ws}"

    async def aexecute(
        self, command: str, *, timeout: int | None = None, exec_id: str | None = None
    ) -> ExecuteResponse:
        key = f"{self._run}:{exec_id}"
        ep = await self._ensure()
        notice = self._notice(ep)
        rec = self._mem.execs.get(key)
        if rec is None:
            execd = ExecdClient(ep.url, ep.token, self._transport)
            if FOREGROUND:
                return await self._foreground(execd, key, command, timeout)
            cmd_id = await execd.start(command, cwd="/workspace", timeout_s=timeout)
            rec = self._mem.execs[key] = ExecRecord(ep.sandbox_id, cmd_id)
            say(
                "PodSandbox",
                f"{key} 提交到 {ep.sandbox_id}，命令 id {cmd_id}（后台：命令不随连接走）",
            )
        else:
            say("PodSandbox", f"{key} 已经提交过（{rec.cmd_id}）→ 接上，不重跑")
        routes = self._ensurer.routes
        routes.running(self._ws).add(key)
        try:
            exit_code = await self._follow(rec, ep, key)
        except _Gone as exc:
            raise SandboxUnavailable(
                "命令执行期间沙箱重启（可能被回收或节点故障），命令已中断、结果丢失；"
                "/workspace 里已经落盘的文件还在"
            ) from exc
        except asyncio.CancelledError:
            say("PodSandbox", f"{key} 这一轮被取消 → DELETE /command（结束沙箱里的进程组）")
            await ExecdClient(ep.url, ep.token, self._transport).interrupt(rec.cmd_id)
            raise
        finally:
            routes.running(self._ws).discard(key)
            routes.touch(self._ws)
        text, truncated = rec.out.text()
        return ExecuteResponse(notice + text, exit_code, truncated)

    # ------------------------------------------------------------------ 内部

    async def _ensure(self, *, fresh: bool = False) -> Endpoint:
        try:
            return await self._ensurer.ensure(
                SBX,
                thread=self._ws,
                user=self._user,
                image=DEFAULT_IMAGE,
                spec=lambda token: sbx_spec(
                    ws=self._ws, user=self._user, token=token, image=DEFAULT_IMAGE
                ),
                port=EXECD_PORT,
                fresh=fresh,
            )
        except (QuotaExceeded, ImageDenied, httpx.HTTPError) as exc:
            raise SandboxUnavailable(f"沙箱启动失败：{exc}") from exc

    def _notice(self, ep: Endpoint) -> str:
        previous = self._mem.seen.get(self._ws)
        self._mem.seen[self._ws] = ep.sandbox_id
        if previous and previous != ep.sandbox_id:
            say(
                "PodSandbox", f"沙箱从 {previous} 换成了 {ep.sandbox_id} → 结果前提示「环境已重建」"
            )
            return (
                "[沙箱环境已重建：/workspace 内的文件仍在；HOME、/repos 里的内容和后台进程已丢失]\n"
            )
        return ""

    async def _follow(self, rec: ExecRecord, ep: Endpoint, key: str) -> int | None:
        delay, attempts = POLL_MIN_S, 0
        while True:
            try:
                execd = ExecdClient(ep.url, ep.token, self._transport)
                chunk, rec.cursor = await execd.logs(rec.cmd_id, rec.cursor)
                rec.out.add(chunk)
                status = await execd.status(rec.cmd_id)
                if status is None:
                    raise _Gone(rec.cmd_id)
                if not status["running"]:
                    chunk, rec.cursor = await execd.logs(rec.cmd_id, rec.cursor)  # 收尾
                    rec.out.add(chunk)
                    return status["exit_code"]
                delay = POLL_MIN_S if chunk else min(delay * 2, POLL_MAX_S)
                attempts = 0
                await asyncio.sleep(delay)
            except httpx.TransportError as exc:
                attempts += 1
                if attempts > RECONNECT_ATTEMPTS:
                    raise SandboxUnavailable("与沙箱的连接中断且无法恢复，命令结果未知") from exc
                say(
                    "PodSandbox",
                    f"{key} 轮询失败（{exc}）→ 重新 ensure，"
                    f"用同一个命令 id、游标 {rec.cursor} 接上",
                )
                await asyncio.sleep(0.05 * attempts)
                ep = await self._ensure(fresh=True)
                if ep.sandbox_id != rec.sandbox_id:
                    status = await ExecdClient(ep.url, ep.token, self._transport).status(rec.cmd_id)
                    answer = "404" if status is None else "居然认识（不该发生）"
                    say(
                        "PodSandbox",
                        f"{key} 现在是 {ep.sandbox_id}，它的 execd 查 {rec.cmd_id} → {answer}",
                    )
                    raise _Gone(rec.cmd_id) from exc

    async def _foreground(
        self, execd: ExecdClient, key: str, command: str, timeout: int | None
    ) -> ExecuteResponse:
        """实验 1 的对照：前台模式。输出跟着连接走，连接一断，execd 就把命令杀了。"""
        out = Output()
        try:
            async for event in execd.run_foreground(command, cwd="/workspace", timeout_s=timeout):
                if event["type"] == "stdout":
                    out.add(event["text"].encode())
                elif event["type"] == "execution_complete":
                    return ExecuteResponse(out.text()[0], event["exit_code"])
        except httpx.TransportError as exc:
            say("PodSandbox", f"{key} 前台连接断了：命令已随连接被杀，接不回来")
            raise SandboxUnavailable("连接中断，前台命令已被结束，结果未知") from exc
        raise SandboxUnavailable("前台流结束但没有收到完成事件")
