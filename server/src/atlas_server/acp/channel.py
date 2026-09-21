"""AcpChannel —— server 到 Pod 内 bridge 的一条 WS。

★ 它**不理解 ACP 语义**，只搬帧：发请求、按 id 关联响应、把通知与反向
  请求交给回调。方法级语义（什么时候 load、什么时候 new）在 runtime。
  分开的理由是可测性：传输层的关联与超时能脱离会话逻辑单测。

与 bridge 的 AdapterProcess 刻意同形（一个跑 WS、一个跑 stdio）——
两边的错误处理与唤醒纪律因此可以一眼比对。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from atlas_acp.wire import is_notification
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

__all__ = ["AcpChannel", "ChannelClosed"]


class ChannelClosed(RuntimeError):
    """到 bridge 的连接断了或从未建立。"""


class AcpChannel:
    """一次 run 一条。用作 async context manager。"""

    def __init__(
        self,
        url: str,
        *,
        token: str,
        thread_id: str,
        on_notification: Callable[[dict[str, Any]], Awaitable[None]],
        on_request: Callable[[dict[str, Any]], Awaitable[Any]],
        connect_timeout_s: float = 10.0,
    ) -> None:
        self._url = url
        self._token = token
        self._thread_id = thread_id
        self._on_notification = on_notification
        self._on_request = on_request
        self._connect_timeout_s = connect_timeout_s
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._closed_reason: str | None = None

    # ------------------------------------------------------------------ 生命周期

    async def __aenter__(self) -> AcpChannel:
        try:
            async with asyncio.timeout(self._connect_timeout_s):
                self._ws = await connect(
                    self._url,
                    additional_headers={
                        # 每 Pod 一份凭证 + 会话绑定。bridge 在握手上校验，
                        # 不通过直接拒 —— K8s 的网络是平的，「在集群里」
                        # 不是安全边界（执行环境 §09）。
                        "Authorization": f"Bearer {self._token}",
                        "X-Thread-Id": self._thread_id,
                    },
                    max_size=8 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                )
        except (TimeoutError, OSError, ConnectionClosed, Exception) as exc:
            msg = f"连不上 bridge（{self._url}）：{exc}"
            raise ChannelClosed(msg) from exc
        self._reader = asyncio.create_task(self._read_loop(), name="acp-channel-read")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._wake_pending(ChannelClosed(self._closed_reason or "通道已关闭"))

    # ------------------------------------------------------------------ 收发

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        if self._ws is None:
            msg = "通道未建立"
            raise ChannelClosed(msg)
        self._next_id += 1
        frame_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = future
        await self._send({"jsonrpc": "2.0", "id": frame_id, "method": method, "params": params})
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._pending.pop(frame_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """发通知不等响应（session/cancel）。

        ★ 通道已断时**吞掉**而不是抛：取消一个连不上的 Pod 里的轮次，
          本来就没有别的可做，把异常抛给取消路径只会掩盖真正的失败原因。
        """
        if self._ws is None:
            return
        with contextlib.suppress(Exception):
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def reply(self, frame_id: Any, result: Any) -> None:
        """回应 bridge 的反向请求（权限决定）。"""
        await self._send({"jsonrpc": "2.0", "id": frame_id, "result": result})

    async def _send(self, frame: dict[str, Any]) -> None:
        if self._ws is None:
            msg = "通道未建立"
            raise ChannelClosed(msg)
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    # ------------------------------------------------------------------ 读循环

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                frame = _parse(raw)
                if frame is None:
                    continue

                # ★ 只有**反向请求**才起任务。它会阻塞很久 —— 权限要等人点头，
                #   上限是 approval_timeout_s（默认 600s）—— 在循环里 await
                #   等于读循环停摆，审批回传堵在 update 流后面
                #   （acp 详设 §12 步骤 3 的实现记录）。
                #
                # ★ 通知与响应**就地处理**，不起任务。两者都不阻塞：前者只是
                #   往无界队列里塞一帧，后者只是 set_result。而为它们起任务要
                #   付出的代价是**乱序** —— 任务各自被调度，通知与响应之间
                #   就没有先后可言了。
                #
                #   顺序在这里不是洁癖：session/load 按 ACP 规定要把历史会话
                #   以 session/update 重放一遍，再回响应。调用方正是靠「响应
                #   到了 ⇒ 重放已经收完」来划分「哪些帧属于上一轮」的边界。
                #   乱序会让边界漏掉几帧，表现为**本轮回答里混进上一轮的话**。
                if "method" in frame and not is_notification(frame):
                    asyncio.create_task(self._handle_request(frame))  # noqa: RUF006
                else:
                    await self._dispatch(frame)
        except ConnectionClosed as exc:
            self._closed_reason = f"bridge 断开（code={exc.rcvd.code if exc.rcvd else '?'}）"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._closed_reason = f"通道异常：{exc}"
        self._wake_pending(ChannelClosed(self._closed_reason or "bridge 断开"))

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        """响应与通知 —— 都不阻塞，由读循环就地调用。"""
        if "method" not in frame:
            future = self._pending.pop(frame.get("id"), None)  # type: ignore[arg-type]
            if future is None or future.done():
                return
            if frame.get("error") is not None:
                future.set_exception(_rpc_error(frame["error"]))
            else:
                future.set_result(frame.get("result"))
            return

        await self._on_notification(frame)

    async def _handle_request(self, frame: dict[str, Any]) -> None:
        """bridge 的反向请求 —— 唯一的是 session/request_permission。

        会一直阻塞到有人做出决定，所以由读循环起任务跑。
        """
        try:
            result = await self._on_request(frame)
        except Exception as exc:  # noqa: BLE001
            logger.warning("处理 bridge 反向请求失败：%s", exc)
            return
        with contextlib.suppress(Exception):
            await self.reply(frame.get("id"), result)

    def _wake_pending(self, error: Exception) -> None:
        """断线时还在等响应的调用方必须被唤醒 —— 否则表现为「CLI 没反应」，
        而实际上连接早没了，是最难查的一种失败。"""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


def _parse(raw: str | bytes) -> dict[str, Any] | None:
    """解析一帧；不是 JSON 对象就丢掉。

    从 _dispatch 里拆出来，是因为读循环要先看清帧的类型才能决定
    「就地处理还是起任务」。
    """
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("bridge 发来非 JSON 帧")
        return None
    return frame if isinstance(frame, dict) else None


def _rpc_error(payload: Any) -> Exception:
    if isinstance(payload, dict):
        return ChannelClosed(f"bridge 返回错误 {payload.get('code')}：{payload.get('message')}")
    return ChannelClosed(f"bridge 返回错误：{payload}")
