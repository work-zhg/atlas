"""Redis 里的两张表：在线表（PresenceTable）、会话登记（BindingStore）。

★ 多个网关实例共享；都带过期：实例崩溃时它写的在线条目自己消失，没人用的会话登记最终被清掉。
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

__all__ = ["BindingStore", "PresenceTable"]

_PRESENCE = "devicegw:presence:{}"
_SESSION = "devicegw:session:{}"
_TOKEN = "devicegw:token:{}"
_DEVICE = "devicegw:device:{}"

#: 只续 / 只删本实例写的那条：设备换到别的实例重连后，旧实例的清理不能动新条目
_TOUCH_IF_MINE = """
local v = redis.call('GET', KEYS[1])
if v and cjson.decode(v)['link'] == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_DROP_IF_MINE = """
local v = redis.call('GET', KEYS[1])
if v and cjson.decode(v)['link'] == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class PresenceTable:
    """设备 → {device, owner, name, instance, link, report, connected_at}。"""

    def __init__(self, redis: aioredis.Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl = ttl_s

    async def put(self, presence: dict[str, Any]) -> None:
        await self._redis.set(
            _PRESENCE.format(presence["device"]), json.dumps(presence), ex=self._ttl
        )

    async def touch(self, device: str, link: str) -> bool:
        return bool(
            await self._redis.eval(_TOUCH_IF_MINE, 1, _PRESENCE.format(device), link, self._ttl)
        )

    async def get(self, device: str) -> dict[str, Any] | None:
        raw = await self._redis.get(_PRESENCE.format(device))
        return json.loads(raw) if raw else None

    async def drop(self, device: str, link: str) -> None:
        await self._redis.eval(_DROP_IF_MINE, 1, _PRESENCE.format(device), link)


class BindingStore:
    """会话登记：按会话、按令牌摘要、按设备三个索引。"""

    def __init__(self, redis: aioredis.Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl = ttl_s

    async def save(self, binding: dict[str, Any]) -> None:
        thread, device = binding["thread"], binding["device"]
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(_SESSION.format(thread), json.dumps(binding), ex=self._ttl)
            pipe.set(_TOKEN.format(binding["token_hash"]), thread, ex=self._ttl)
            pipe.sadd(_DEVICE.format(device), thread)
            pipe.expire(_DEVICE.format(device), self._ttl)
            await pipe.execute()

    async def by_thread(self, thread: str) -> dict[str, Any] | None:
        raw = await self._redis.get(_SESSION.format(thread))
        return json.loads(raw) if raw else None

    async def by_token(self, token_hash: str) -> dict[str, Any] | None:
        thread = await self._redis.get(_TOKEN.format(token_hash))
        if thread is None:
            return None
        binding = await self.by_thread(thread.decode() if isinstance(thread, bytes) else thread)
        # 令牌换过（会话被作废后重新登记）：旧令牌的索引指向的已不是它
        if binding is None or binding.get("token_hash") != token_hash:
            return None
        return binding

    async def touch(self, binding: dict[str, Any]) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.expire(_SESSION.format(binding["thread"]), self._ttl)
            pipe.expire(_TOKEN.format(binding["token_hash"]), self._ttl)
            await pipe.execute()

    async def threads_of(self, device: str) -> list[str]:
        members = await self._redis.smembers(_DEVICE.format(device))
        return sorted(m.decode() if isinstance(m, bytes) else m for m in members)

    async def delete(self, thread: str) -> None:
        binding = await self.by_thread(thread)
        if binding is None:
            return
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(_SESSION.format(thread))
            pipe.delete(_TOKEN.format(binding["token_hash"]))
            pipe.srem(_DEVICE.format(binding["device"]), thread)
            await pipe.execute()
