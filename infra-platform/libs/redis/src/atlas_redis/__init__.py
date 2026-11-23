"""Atlas Redis 客户端中间件（c4-model/infra-platform/design-redis.html）。

统一提供：
  · RedisRuntime / RedisConfig  —— 三路连接舱壁隔离（commands / blocking / pubsub）与生命周期
  · KeySpace                    —— 强类型键空间（服务前缀、租户隔离、Cluster hash_tag、脱敏模式）
  · RedisClient / Middleware    —— Guard / Telemetry / Resilience 可插拔拦截器链与分级容灾
  · 高阶原语                    —— ScriptRegistry、PubSubHub、StreamChannel、AtomicQuota、
                                   CoalescedCache，以及经 Runtime 工厂复用的 DistributedLock
"""

from atlas_redislock import LockBusy, LockError, LockLost, RedisLock

from .cache import CoalescedCache
from .client import RedisClient, TrackedPipeline
from .config import RedisConfig
from .errors import (
    CircuitOpen,
    CommandForbidden,
    PayloadTooLarge,
    RedisMiddlewareError,
    RedisUnavailable,
)
from .keyspace import KeySpace
from .middleware import (
    FAIL_OPEN_FALLBACK,
    CircuitBreaker,
    CommandContext,
    GuardMiddleware,
    NextCall,
    RedisMiddleware,
    ResilienceMiddleware,
    TelemetryMiddleware,
    TelemetrySnapshot,
)
from .pubsub import PubSubHub, Resubscribed
from .quota import AtomicQuota, QuotaResult
from .runtime import RedisRuntime
from .scripts import ScriptHandle, ScriptRegistry
from .stream import StreamChannel, StreamRecord

DistributedLock = RedisLock

__all__ = [
    "FAIL_OPEN_FALLBACK",
    "AtomicQuota",
    "CircuitBreaker",
    "CircuitOpen",
    "CoalescedCache",
    "CommandContext",
    "CommandForbidden",
    "DistributedLock",
    "GuardMiddleware",
    "KeySpace",
    "LockBusy",
    "LockError",
    "LockLost",
    "NextCall",
    "PayloadTooLarge",
    "PubSubHub",
    "QuotaResult",
    "RedisClient",
    "RedisConfig",
    "RedisLock",
    "RedisMiddleware",
    "RedisMiddlewareError",
    "RedisRuntime",
    "RedisUnavailable",
    "ResilienceMiddleware",
    "Resubscribed",
    "ScriptHandle",
    "ScriptRegistry",
    "StreamChannel",
    "StreamRecord",
    "TelemetryMiddleware",
    "TelemetrySnapshot",
    "TrackedPipeline",
]
