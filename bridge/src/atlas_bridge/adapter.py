"""AdapterProcess —— stdio 上驱动 CLI adapter 的子进程。

ACP 在 stdio 上跑的是**行分隔的 JSON**（一帧一行）。Bridge 是 ACP client：
它拉起 adapter，adapter 再启动并管理真正的 CLI 进程 —— 第三层对我们是黑盒
（执行环境 §01）。

★ Bridge 只能和它自己拉起的 adapter 对话。不做「连接已有进程」——
  那需要另一套发现与鉴权机制，而 Pod-per-session 下根本没有这个场景。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from atlas_acp.wire import ErrorCode, is_notification

logger = logging.getLogger(__name__)

__all__ = ["AdapterCrashed", "AdapterProcess"]

#: 单帧上限。adapter 疯掉时一行几百 MB 会把 Pod 内存打爆，
#: 而 Pod 的 OOMKill 不给优雅终止的机会（连崩溃上报都发不出去）。
_MAX_FRAME_BYTES = 8 * 1024 * 1024


class AdapterCrashed(RuntimeError):
    """adapter 进程异常退出。

    ★ 不在进程内自愈。崩溃后 bridge 上报一条 bridge/adapter_crashed 然后
      退出，让 Pod 的重启策略接管 —— 进程内重启会掩盖崩溃频率，且一个
      半死的 adapter 的会话状态无法验证（执行环境 §04）。
    """


class AdapterProcess:
    """拉起 adapter，在 stdio 上收发 JSON-RPC 帧。

    三类入站帧各有去处：
      Response（带 id）      → 解开对应的等待
      Notification          → 交给 on_notification（bridge 转发给 server）
      Request（adapter 发起）→ 交给 on_request，把结果回写 stdin
                              （唯一的反向请求是 session/request_permission）
    """

    def __init__(
        self,
        command: list[str],
        *,
        on_notification: Callable[[dict[str, Any]], Awaitable[None]],
        on_request: Callable[[dict[str, Any]], Awaitable[Any]],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._on_notification = on_notification
        self._on_request = on_request
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        #: 崩溃后由 wait_closed 的等待方取走 —— bridge 据此上报并退出。
        self._crash: AdapterCrashed | None = None
        #: 主动停止中 —— terminate 给出的非零退出码不算崩溃。
        self._stopping = False
        self._stderr_pump: asyncio.Task[None] | None = None
        #: stderr 的最后若干行 —— 崩溃上报时带上。有界，免得长跑进程吃内存。
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._closed = asyncio.Event()

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # ★ stderr 不并进 stdout：那条流上跑的是 JSON 帧，
            #   adapter 打一行日志就会让解析失败。
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._reader = asyncio.create_task(self._read_loop(), name="acp-adapter-read")
        # ★ stderr 必须**一直**抽干，不能只在进程死后读。两个理由，
        #   第二个是硬伤：
        #
        #   ① 可观测性：adapter 活着时出的错（比如 session/load 失败）只会
        #     写 stderr，然后原地留在管道里 —— 日志里一个字都没有，在 Pod
        #     里根本没法查。
        #   ② 管道写满会**阻塞 adapter**。PIPE 的缓冲区只有 64KB，没人读就
        #     满，满了 adapter 下一次写 stderr 就卡死 —— 表现是 CLI 毫无反应，
        #     而进程还活得好好的。真 CLI 比假 adapter 话多得多，这条迟早撞上。
        self._stderr_pump = asyncio.create_task(self._drain_stderr(), name="acp-adapter-stderr")

    async def _drain_stderr(self) -> None:
        """把 adapter 的 stderr 逐行抽到日志，并留最后几行给崩溃原因用。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                logger.warning("adapter stderr: %s", text[:500])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 抽日志不该反过来搞挂 bridge
            logger.debug("adapter stderr 抽取中断", exc_info=True)

    async def stop(self) -> None:
        """主动停止。

        ★ 顺序要紧：**先终止进程，再 cancel 读循环**。反过来会死锁 ——
          读循环的收尾要读 stderr 到 EOF，而 EOF 要等进程退出，退出又等
          着这个方法往下走。踩过一次，表现是测试整体挂住而非报错。
        """
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                async with asyncio.timeout(5):
                    await proc.wait()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()

        if self._stderr_pump is not None:
            self._stderr_pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_pump

        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        self._wake_pending(AdapterCrashed("adapter 已停止"))
        self._closed.set()

    @property
    def crash(self) -> AdapterCrashed | None:
        return self._crash

    async def wait_closed(self) -> AdapterCrashed | None:
        """等到 adapter 退出。返回崩溃原因（正常退出为 None）。"""
        await self._closed.wait()
        return self._crash

    # ------------------------------------------------------------------ 收发

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        """发一个请求并等响应。超时按崩溃处理 —— 卡死的 adapter 与死掉的
        adapter 对调用方是同一件事，都得让上层看见。"""
        proc = self._proc
        if proc is None or proc.stdin is None:
            msg = "adapter 未启动"
            raise AdapterCrashed(msg)

        self._next_id += 1
        frame_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = future
        await self._write({"jsonrpc": "2.0", "id": frame_id, "method": method, "params": params})
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._pending.pop(frame_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """发一个通知，不等响应（如 session/cancel）。"""
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            msg = "adapter stdin 不可用"
            raise AdapterCrashed(msg)
        proc.stdin.write(json.dumps(frame, ensure_ascii=False).encode() + b"\n")
        await proc.stdin.drain()

    # ------------------------------------------------------------------ 读循环

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # 单帧超出缓冲上限。丢弃这一行继续 —— 但它通常预示
                    # adapter 已经不正常了，日志要留痕。
                    logger.warning("adapter 帧超长，已丢弃")
                    continue
                if not line:
                    break  # EOF：进程退出
                if len(line) > _MAX_FRAME_BYTES:
                    logger.warning("adapter 帧超过 %d 字节，已丢弃", _MAX_FRAME_BYTES)
                    continue
                await self._dispatch(line)
        except asyncio.CancelledError:
            # ★ 不在这里收尾：收尾要 await（读 stderr），而被取消的任务里
            #   再 await 极易二次取消。主动停止的收尾归 stop()。
            raise
        except Exception:
            logger.exception("adapter 读循环异常")
        await self._on_exit()

    async def _dispatch(self, line: bytes) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            # ★ 解析失败不杀连接：adapter 可能往 stdout 漏了一行非 JSON
            #   （常见于第三方 CLI 的启动横幅）。记一条就继续。
            logger.warning("adapter 输出了非 JSON 行：%.200s", line.decode("utf-8", "replace"))
            return
        if not isinstance(frame, dict):
            return

        if "method" in frame:
            if is_notification(frame):
                await self._on_notification(frame)
                return
            await self._serve_request(frame)
            return

        # Response：解开等待。对不上 id 的响应丢弃 —— 它对应的请求已超时。
        future = self._pending.pop(frame.get("id"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if frame.get("error") is not None:
            future.set_exception(_rpc_error(frame["error"]))
        else:
            future.set_result(frame.get("result"))

    async def _serve_request(self, frame: dict[str, Any]) -> None:
        """adapter 发起的请求 —— 目前只有 session/request_permission。"""
        frame_id = frame.get("id")
        try:
            result = await self._on_request(frame)
            await self._write({"jsonrpc": "2.0", "id": frame_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            logger.warning("处理 adapter 请求失败：%s", exc)
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": frame_id,
                    "error": {"code": ErrorCode.INTERNAL_ERROR, "message": str(exc)},
                }
            )

    async def _on_exit(self) -> None:
        proc = self._proc
        code = proc.returncode
        if code is None and proc is not None:
            # stdout 到了 EOF 但进程还没被收割 —— 等一下拿真实退出码，
            # 否则崩溃原因里只有一个 None，排查时等于没有。
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    code = await proc.wait()

        # ★ 不在这里读 stderr —— _drain_stderr 一直在读，两边同时读会打架。
        #   给泵一点时间把剩下的收完，再取环形缓冲里的尾巴。
        if self._stderr_pump is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                async with asyncio.timeout(2):
                    await self._stderr_pump
        stderr_tail = "\n".join(self._stderr_tail)

        if code not in (0, None) and not self._stopping:
            # ★ 主动 stop() 里的 terminate 也会给出非零退出码 —— 那不是崩溃。
            self._crash = AdapterCrashed(f"adapter 退出码 {code}：{stderr_tail.strip()[:500]}")
            logger.error("adapter 崩溃：%s", self._crash)

        self._wake_pending(self._crash or AdapterCrashed("adapter 已退出"))
        self._closed.set()

    def _wake_pending(self, error: Exception) -> None:
        """还在等响应的调用方必须被唤醒，否则各自挂到超时 ——
        「CLI 没反应」而进程其实早没了，是最难查的一种失败。"""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


def _rpc_error(payload: Any) -> Exception:
    if isinstance(payload, dict):
        return AdapterCrashed(f"adapter 返回错误 {payload.get('code')}：{payload.get('message')}")
    return AdapterCrashed(f"adapter 返回错误：{payload}")
