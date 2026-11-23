"""Redis 客户端的唯一构造点。

★ 为什么必须集中：redis-py 8 把默认 `socket_timeout` 从 None 改成了 **5 秒**
（`connection.py` 的 DEFAULT_SOCKET_TIMEOUT）。我们的两类阻塞读都长于它：

  · SSE 中继的 `XREAD block=15s`（sse_heartbeat_s）
  · 审批门禁的 `BLPOP timeout=5s`（正好压线，是竞态）

于是「两个事件间隔超过 5 秒」的慢 run 会让 SSE 连接被客户端自己掐断，
表现为前端 RemoteProtocolError、服务端一条指向 relay.tail 的 TimeoutError ——
离病根（一个依赖库的默认值变更）非常远。此前散落三处的 from_url
各自吃默认值，这个坑就藏在那里。

取舍：socket_timeout=None + 连接超时 5s。
  · 阻塞命令都带服务端上限（block/timeout 参数），Redis 会按时回话，
    None 不会造成无界挂起
  · 真正的断连由 health_check_interval 与连接超时兜住
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import redis.asyncio as aioredis

from .config import Settings, get_settings

#: 两类连接共用的参数（见上面的取舍）
_CONNECTION: dict[str, Any] = {
    "decode_responses": True,
    "socket_timeout": None,
    "socket_connect_timeout": 5,
    "health_check_interval": 30,
}


def make_redis(settings: Settings) -> aioredis.Redis:
    """一个专用的客户端，用完由调用方 aclose()。

    只给低频、或者要独占一条连接的地方（授权失效的订阅、退役沙箱、浏览器画面这类）。
    高频的用 RedisComponent 的共享连接池。
    """
    return aioredis.from_url(settings.redis_url, **_CONNECTION)


class RedisComponent:
    """进程级的 Redis 连接：两个共享连接池，调用方拿客户端来用、**不关**。

        commands  短命令：发布事件、分配序号、锁、取消位、续租……每个请求、每段 run 都用它
        streams   会话事件流的阻塞读（XREAD block）：一条连接一占就是一个心跳间隔，和短命令
                  分开，挂着的事件流再多也占不光短命令的连接

    ★ 为什么要共享：以前每个请求、每段 run、每次续租都 from_url 建一个客户端（自带一个连接池）、
      用完再关 —— 每次都是新的 TCP 连接，高并发时连接数跟着请求数走，Redis 那边一直在建连、断连。
    ★ 两个池都是 BlockingConnectionPool：连接用满时等空出来的（最多 redis_pool_timeout_s），而不是
      像默认的池那样立刻报「连接太多」。
    ★ 和数据库引擎一样是进程级单例（get_redis_component）：连接绑定事件循环，测试里每个用例一个
      循环，用例结束时关掉并清缓存（tests/conftest.py）。
    """

    def __init__(self, settings: Settings) -> None:
        self.commands = self._client(settings, settings.redis_max_connections)
        # 事件流的连接数由 SSE 闸门封顶（sse_max_connections），多留几条给重连的交接
        self.streams = self._client(settings, settings.sse_max_connections + 16)

    @staticmethod
    def _client(settings: Settings, max_connections: int) -> aioredis.Redis:
        pool = aioredis.BlockingConnectionPool.from_url(
            settings.redis_url,
            max_connections=max_connections,
            timeout=settings.redis_pool_timeout_s,
            **_CONNECTION,
        )
        return aioredis.Redis(connection_pool=pool)

    async def aclose(self) -> None:
        """进程退出时关（lifespan 结束）。"""
        for client in (self.commands, self.streams):
            await client.aclose()
            await client.connection_pool.disconnect()


@lru_cache
def get_redis_component() -> RedisComponent:
    return RedisComponent(get_settings())


def shared_redis() -> aioredis.Redis:
    """共享的短命令客户端（RedisComponent.commands）。★ 不要 aclose。"""
    return get_redis_component().commands
