"""Pod 里的 execd（OpenSandbox 的数据面，模拟）：HTTP 接口 + 真实地执行命令。

只实现 Atlas 用到的接口（specs/execd-api.yaml 的子集），行为照 execd 的源码：

    POST   /command              background=true：建进程、立刻回 init 事件（带命令 id），
                                 连接随即结束
                                 background=false：输出跟着 SSE 走，★ 连接断开 → SIGKILL 整个进程组
    GET    /command/status/{id}  running / exit_code；不认识的 id → 404（记录只在内存里）
    GET    /command/{id}/logs    游标之后的全部输出 + EXECD-COMMANDS-TAIL-CURSOR（★ 字节偏移）
    DELETE /command?id=          SIGTERM 整个进程组，宽限后 SIGKILL
    GET    /files/download       读 Pod 里的文件（只认挂载过的目录）
    POST   /files/upload         写 Pod 里的文件；写完 close → 写穿（Pod 调挂载的 flush）
    GET    /ping、/ready          不要令牌

★ 命令真的由 bash 执行：start_new_session（自成进程组）、stdin=/dev/null、stdout 与 stderr 合并进
  $TMPDIR 下的日志文件；子进程环境里没有 execd 自己的配置和令牌（execd 的 filterEnvBlacklist）。
★ execd 自己没有「卡死判定」「输出截断」「幂等」—— 这些在 server 侧的 PodSandbox 里（设计 §6）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tracing import say

PORT = 44772
TOKEN_HEADER = "x-execd-access-token"
INTERRUPT_GRACE_S = 0.3


@dataclass
class Command:
    id: str
    content: str
    proc: asyncio.subprocess.Process
    log: Path
    background: bool
    exit_code: int | None = None

    @property
    def running(self) -> bool:
        return self.exit_code is None


class Execd:
    def __init__(
        self,
        *,
        pod: str,
        token: str,
        resolve: Callable[[str], Path],
        env: dict[str, str],
        on_close: Callable[[], None],
    ) -> None:
        self.pod, self._token, self._resolve = pod, token, resolve
        # ★ 子进程环境：去掉 execd 自己的配置与令牌
        self._env = {k: v for k, v in env.items() if not k.startswith("EXECD_")}
        self._on_close = on_close  # 文件 close → 写穿（CSI 驱动的事，这里只是通知它）
        self._logs = resolve("/tmp") / ".execd"
        self._logs.mkdir(parents=True, exist_ok=True)
        self.commands: dict[str, Command] = {}
        self._next = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in ("/ping", "/ready"):
            return httpx.Response(200)
        if request.headers.get(TOKEN_HEADER) != self._token:
            say("execd", f"{self.pod} 令牌不对 → 401")
            return httpx.Response(401, json={"code": "UNAUTHORIZED"})
        method = request.method
        if method == "POST" and path == "/command":
            body = json.loads(request.content)
            cmd = await self._start(body)
            return httpx.Response(
                200,
                stream=_Events(self._events(cmd)),
                headers={"content-type": "text/event-stream"},
            )
        if method == "GET" and path.startswith("/command/status/"):
            cmd = self.commands.get(path.rsplit("/", 1)[1])
            if cmd is None:
                return httpx.Response(404, json={"code": "NOT_FOUND"})
            return httpx.Response(
                200,
                json={
                    "id": cmd.id,
                    "running": cmd.running,
                    "exit_code": cmd.exit_code,
                    "content": cmd.content,
                },
            )
        if method == "GET" and path.endswith("/logs"):
            cmd = self.commands.get(path.split("/")[2])
            if cmd is None:
                return httpx.Response(404, json={"code": "NOT_FOUND"})
            data = cmd.log.read_bytes()
            cursor = min(int(request.url.params.get("cursor", 0)), len(data))
            return httpx.Response(
                200, content=data[cursor:], headers={"EXECD-COMMANDS-TAIL-CURSOR": str(len(data))}
            )
        if method == "DELETE" and path == "/command":
            cmd = self.commands.get(request.url.params["id"])
            if cmd is not None and cmd.running:
                asyncio.get_running_loop().create_task(self._interrupt(cmd))
            return httpx.Response(200)
        if method == "GET" and path == "/files/download":
            target = self._resolve(request.url.params["path"])
            return (
                httpx.Response(200, content=target.read_bytes())
                if target.is_file()
                else httpx.Response(404)
            )
        if method == "POST" and path == "/files/upload":
            body = json.loads(request.content)
            target = self._resolve(body["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body["content"])
            self._on_close()  # 写完 close：s3fs 在这里同步上传
            return httpx.Response(200)
        return httpx.Response(404)

    def kill_all(self) -> None:
        """Pod 被删：容器里的进程全没了。"""
        for cmd in self.commands.values():
            if cmd.running:
                _killpg(cmd, signal.SIGKILL)

    # ------------------------------------------------------------------ 执行

    async def _start(self, body: dict[str, Any]) -> Command:
        self._next += 1  # ★ 在任何 await 之前分配：同一批并行提交的命令不能拿到同一个 id
        cmd_id = f"cmd-{self._next}"
        log = self._logs / f"{cmd_id}.log"
        with log.open("wb") as out:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-lc", body["command"],
                cwd=self._resolve(body.get("cwd", "/workspace")),
                env=self._env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # 自成进程组：结束时杀整组
            )  # fmt: skip
        cmd = Command(cmd_id, body["command"], proc, log, bool(body.get("background")))
        self.commands[cmd_id] = cmd
        mode = "后台" if cmd.background else "前台"
        say("execd", f"{self.pod} {cmd_id} 开始（{mode}，进程组 {proc.pid}）")
        timeout = body.get("timeout")
        asyncio.get_running_loop().create_task(self._wait(cmd, timeout / 1000 if timeout else None))
        return cmd

    async def _wait(self, cmd: Command, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(cmd.proc.wait(), timeout)
        except TimeoutError:
            say(
                "execd",
                f"{self.pod} {cmd.id} 到了调用方给的 timeout（{timeout:g} 秒）→ 杀整个进程组",
            )
            _killpg(cmd, signal.SIGKILL)
            await cmd.proc.wait()
        code = cmd.proc.returncode
        cmd.exit_code = 128 - code if code < 0 else code  # 被信号杀掉：按 shell 惯例报 128+信号
        self._on_close()  # 命令写的文件都 close 了：写穿
        say("execd", f"{self.pod} {cmd.id} 退出，exit {cmd.exit_code}")

    async def _events(self, cmd: Command):
        yield _sse({"type": "init", "text": cmd.id})
        if cmd.background:
            return  # 后台：连接到此结束，命令和它无关
        try:  # 前台：边跑边推输出
            cursor = 0
            while True:
                try:
                    data = cmd.log.read_bytes()
                except FileNotFoundError:
                    return  # Pod 被删了：$TMPDIR 随 emptyDir 一起没了，连接也就断了
                if len(data) > cursor:
                    yield _sse({"type": "stdout", "text": data[cursor:].decode(errors="replace")})
                    cursor = len(data)
                if not cmd.running:
                    yield _sse({"type": "execution_complete", "exit_code": cmd.exit_code})
                    return
                await asyncio.sleep(0.05)
        finally:
            if cmd.running:  # ★ execd 的 runtime/command.go：客户端断开 → ctx.Done → SIGKILL 整组
                say("execd", f"{self.pod} {cmd.id} 是前台命令，连接断了 → SIGKILL 整个进程组")
                _killpg(cmd, signal.SIGKILL)

    async def _interrupt(self, cmd: Command) -> None:
        say(
            "execd",
            f"{self.pod} {cmd.id} 中断：SIGTERM 整个进程组，{INTERRUPT_GRACE_S:g} 秒后 SIGKILL",
        )
        _killpg(cmd, signal.SIGTERM)
        await asyncio.sleep(INTERRUPT_GRACE_S)
        if cmd.running:
            _killpg(cmd, signal.SIGKILL)


class _Events(httpx.AsyncByteStream):
    """SSE 响应体。客户端关掉连接时 aclose 被调用 → 生成器的 finally 立刻执行。

    对应 execd 的 ctx.Done()：连接一断它就知道。
    """

    def __init__(self, gen: AsyncIterator[bytes]) -> None:
        self._gen = gen

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._gen:
            yield chunk

    async def aclose(self) -> None:
        await self._gen.aclose()  # type: ignore[attr-defined]


def _killpg(cmd: Command, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(cmd.proc.pid, sig)


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
