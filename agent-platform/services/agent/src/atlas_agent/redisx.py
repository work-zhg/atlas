"""Redis 客户端的唯一构造点（接入 atlas_redis 中间件底座）。

★ 底层由 infra-platform/libs/redis::RedisRuntime 提供三路舱壁隔离：
  · commands  短命令池（socket_timeout=2s）+ Guard / Telemetry / Resilience 中间件链
  · streams   阻塞读池（socket_timeout=None）专供 SSE XREAD BLOCK
  · pubsub    单连接多路复用 PubSubHub（CLI 在线审批、失效通知共用 1 条连接，不再挤占短命令池）
  · scripts   Lua 脚本注册表（EVALSHA 自动回退 EVAL）

★ 低频专用连接（授权失效独立订阅、退役沙箱清理等）仍可走 make_redis(settings) 并自行 aclose()。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import redis.asyncio as aioredis
from atlas_redis import PubSubHub, RedisClient, RedisConfig, RedisRuntime, ScriptRegistry

from .config import Settings, get_settings

#: 独立专用连接的参数（保持 socket_timeout=None 给长监听与阻塞清理）
_CONNECTION: dict[str, Any] = {
    "decode_responses": True,
    "socket_timeout": None,
    "socket_connect_timeout": 5,
    "health_check_interval": 30,
}


def make_redis(settings: Settings) -> aioredis.Redis:
    """一个专用的客户端，用完由调用方 aclose()。

    只给低频、或者要独占一条连接的地方（授权失效的订阅、退役沙箱、浏览器画面这类）。
    高频的用 RedisComponent（底层为 atlas_redis.RedisRuntime）的共享连接池。
    """
    return aioredis.from_url(settings.redis_url, **_CONNECTION)


class RedisComponent:
    """进程级的 Redis 门面：封装 atlas_redis.RedisRuntime，保持向后兼容的属性名。

    commands  短命令（RedisClient，2s 超时 + 拦截链）：发布事件、分配序号、锁、取消位、续租……
    streams   阻塞读（RedisClient，socket_timeout=None）：会话事件流 XREAD BLOCK
    pubsub    单连接多路复用 PubSubHub：CLI 审批通知等不再各自开短命令池连接
    scripts   ScriptRegistry（EVALSHA）
    """

    def __init__(self, settings: Settings) -> None:
        cfg = RedisConfig(
            url=settings.redis_url,
            service="agent",
            command_max_connections=settings.redis_max_connections,
            blocking_max_connections=settings.sse_max_connections + 16,
            pool_wait_timeout_s=settings.redis_pool_timeout_s,
        )
        self.runtime = RedisRuntime(cfg)
        self.commands: RedisClient = self.runtime.commands
        self.streams: RedisClient = self.runtime.blocking
        self.pubsub: PubSubHub = self.runtime.pubsub
        self.scripts: ScriptRegistry = self.runtime.scripts

    async def aclose(self) -> None:
        """进程退出时关（lifespan 结束）。"""
        await self.runtime.aclose()


@lru_cache
def get_redis_component() -> RedisComponent:
    return RedisComponent(get_settings())


def shared_redis() -> Any:
    """共享的短命令客户端（RedisComponent.commands）。★ 不要 aclose。"""
    return get_redis_component().commands


def shared_pubsub() -> PubSubHub:
    """共享的单连接 PubSubHub。★ 不要 stop，由 lifespan 统一停。"""
    return get_redis_component().pubsub


def shared_scripts() -> ScriptRegistry:
    """共享的 Lua 脚本注册表（EVALSHA）。"""
    return get_redis_component().scripts
