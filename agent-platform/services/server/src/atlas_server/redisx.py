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

import redis.asyncio as aioredis

from .config import Settings


def make_redis(settings: Settings) -> aioredis.Redis:
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_connect_timeout=5,
        health_check_interval=30,
    )
