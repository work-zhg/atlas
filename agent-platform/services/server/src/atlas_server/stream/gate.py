"""SSE 并发连接闸门。

★ 为什么需要它。订阅单位从 run 换成 thread 之后，连接的寿命从「一轮」变成
  「用户开着这个会话多久」—— 而且一个用户开三个标签页就是三条。每条 SSE 各
  占一个 Redis 连接（DB 连接在 stream 开头就还掉了，见 RunService.stream_thread）。

  不设限的表现不是「第 N+1 个连不上」，而是**所有 HTTP 请求一起变慢**：
  Redis 连接池耗尽后新命令排队等待，而队列里混着正常请求。根因（一批挂着的
  长连接）离症状很远。

★ 宁可对第 N+1 个明确 503。它带 Retry-After，客户端退避重连即可；而慢到
  超时的请求分不清是该重试还是该放弃。
"""

from __future__ import annotations

import logging
from types import TracebackType

logger = logging.getLogger(__name__)

__all__ = ["StreamGate", "StreamsBusy"]


class StreamsBusy(RuntimeError):
    """并发 SSE 连接已达上限。"""


class StreamGate:
    """进程级的 SSE 连接计数器。

    ★ 刻意是**进程级**而不是全局（Redis）计数。它保护的是本进程的连接池 ——
      那是个进程内资源。用共享计数器反而会让一个空闲进程因为别人满了而拒连。

    ★ 不用 Semaphore：那会让超限的请求**排队等待**，而我们要的正是立刻拒绝。
      排队等一个可能几小时才释放的 SSE 槽位，等于把 503 换成超时。
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    def __enter__(self) -> StreamGate:
        if self._active >= self._limit:
            logger.warning("SSE 连接已达上限 %d，拒绝新订阅", self._limit)
            msg = f"并发事件流连接已达上限（{self._limit}），请稍后重试"
            raise StreamsBusy(msg)
        self._active += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._active -= 1

    def guard(self, generator):
        """把一个异步生成器包进闸门 —— 占用随迭代结束（含被取消）而释放。

        ★ 必须包在生成器外面而不是在路由里 `with`：路由函数在
          StreamingResponse 构造完就返回了，而流还要跑几小时。在路由里占用
          的话计数会在第一帧之前就归零。
        """

        async def wrapped():
            with self:
                async for item in generator:
                    yield item

        return wrapped()
