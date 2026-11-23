"""原子限流与额度累加原语（c4-model/infra-platform/code-redis.html）。

★ 为什么必须用单条 Lua 脚本：
  原先 QuotaGuard 先 INCR 再判断 count==1 调 EXPIRE —— 一旦进程在两条命令之间崩溃或网络超时，
  该 key 会变成永不过期（TTL=-1）的僵尸限流键。本模块把 INCR + TTL 检查/设置（以及 INCRBYFLOAT
  + EXPIRE）合进原子 Lua 并走 ScriptRegistry(EVALSHA)，同时默认采用 fail_open=True 容灾。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .client import RedisClient
from .middleware import FAIL_OPEN_FALLBACK
from .scripts import ScriptRegistry

__all__ = ["AtomicQuota", "QuotaResult"]

_SCRIPT_WINDOW = "atlas_redis.quota.fixed_window"
_LUA_WINDOW = """
local c = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if c == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {c, ttl}
"""

_SCRIPT_SPEND = "atlas_redis.quota.add_spend"
_LUA_SPEND = """
local v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return v
"""


@dataclass(frozen=True, slots=True)
class QuotaResult:
    allowed: bool
    count: int
    limit: int
    retry_after_s: int = 0
    degraded: bool = False


class AtomicQuota:
    """Lua 原子固定窗口限流与额度计数（默认 Fail-Open，不因 Redis 抖动阻断业务）。"""

    def __init__(
        self,
        *,
        commands: RedisClient,
        scripts: ScriptRegistry,
        fail_open: bool = True,
    ) -> None:
        self._client = commands.with_policy(fail_open=fail_open)
        self._scripts = scripts
        self._scripts.register(_SCRIPT_WINDOW, _LUA_WINDOW)
        self._scripts.register(_SCRIPT_SPEND, _LUA_SPEND)

    async def allow(
        self,
        key: str,
        *,
        limit: int,
        window_s: int = 60,
        key_pattern: str = "quota:window",
    ) -> QuotaResult:
        """检查并计入一次请求；limit <= 0 视为不限。"""
        if limit <= 0:
            return QuotaResult(allowed=True, count=0, limit=limit)
        res = await self._scripts.run(
            _SCRIPT_WINDOW,
            keys=(key,),
            args=(str(window_s),),
            client=self._client,
            key_pattern=key_pattern,
        )
        if res is FAIL_OPEN_FALLBACK:
            return QuotaResult(allowed=True, count=0, limit=limit, degraded=True)
        count = int(res[0])
        ttl = max(int(res[1]), 1)
        if count > limit:
            return QuotaResult(allowed=False, count=count, limit=limit, retry_after_s=ttl)
        return QuotaResult(allowed=True, count=count, limit=limit)

    async def add_spend(
        self,
        key: str,
        amount: Decimal,
        *,
        ttl_s: int = 40 * 86_400,
        key_pattern: str = "quota:spend",
    ) -> Decimal | None:
        """原子累加消费额度并续期；Fail-Open 降级时返回 None。"""
        if amount <= 0:
            return Decimal(0)
        res = await self._scripts.run(
            _SCRIPT_SPEND,
            keys=(key,),
            args=(str(amount), str(ttl_s)),
            client=self._client,
            key_pattern=key_pattern,
        )
        if res is FAIL_OPEN_FALLBACK:
            return None
        text = res.decode() if isinstance(res, bytes) else str(res)
        return Decimal(text)
