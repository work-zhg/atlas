"""Redis 中间件配置（c4-model/infra-platform/code-redis.html）。

★ 为什么把短命令和阻塞读的超时分开配：
  redis-py 8 默认 socket_timeout=5s，会误杀 15s 的 XREAD BLOCK；若为此把全部连接都改成
  socket_timeout=None，短命令遇半开 TCP 僵死时会卡满 30s 才被健康检查发现。
  拆池之后，commands 池恢复 2s 严格超时，blocking 池保持 None + 服务端 block 上限。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

__all__ = ["RedisConfig"]

_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None and raw.strip() else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None and raw.strip() else default


@dataclass(frozen=True)
class RedisConfig:
    """一个服务进程的 Redis 连接池与治理参数。"""

    url: str
    service: str

    # 1) 短命令池（commands）：严格短超时，追求高复用与快失败
    command_max_connections: int = 100
    command_socket_timeout_s: float = 2.0
    command_connect_timeout_s: float = 3.0

    # 2) 阻塞读池（blocking）：供 XREAD BLOCK / BLPOP 等使用
    blocking_max_connections: int = 256
    blocking_socket_timeout_s: float | None = None

    # 通用池参数
    pool_wait_timeout_s: float = 5.0
    health_check_interval_s: int = 30
    decode_responses: bool = True

    # 3) 中间件治理参数
    slow_command_ms: float = 50.0
    large_payload_warn_bytes: int = 256 * 1024
    large_payload_max_bytes: int | None = 4 * 1024 * 1024
    forbid_dangerous_commands: bool = True

    # 4) 熔断与 PubSub 参数
    breaker_failure_threshold: int = 5
    breaker_recovery_s: float = 10.0
    pubsub_queue_maxsize: int = 256
    pubsub_backoff_min_s: float = 0.2
    pubsub_backoff_max_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("RedisConfig.url 不能为空")
        if not _SERVICE_RE.match(self.service):
            raise ValueError(
                f"RedisConfig.service 须匹配 {_SERVICE_RE.pattern}，收到：{self.service!r}"
            )
        if self.command_max_connections <= 0 or self.blocking_max_connections <= 0:
            raise ValueError("连接池最大连接数必须大于 0")
        if self.command_socket_timeout_s <= 0 or self.command_connect_timeout_s <= 0:
            raise ValueError("短命令超时与建连超时必须大于 0")
        if self.pool_wait_timeout_s <= 0:
            raise ValueError("pool_wait_timeout_s 必须大于 0")
        if self.breaker_failure_threshold <= 0 or self.breaker_recovery_s <= 0:
            raise ValueError("熔断阈值与恢复窗口必须大于 0")
        if self.pubsub_queue_maxsize <= 0:
            raise ValueError("pubsub_queue_maxsize 必须大于 0")

    @classmethod
    def from_env(
        cls,
        service: str,
        *,
        url: str | None = None,
        env_prefix: str = "REDIS",
        blocking_max_connections: int | None = None,
    ) -> RedisConfig:
        """从环境变量构造标准配置（默认读 REDIS_URL 及同前缀可选调优项）。"""
        resolved_url = url or os.getenv(f"{env_prefix}_URL", "redis://localhost:6379/0")
        return cls(
            url=resolved_url,
            service=service,
            command_max_connections=_env_int(f"{env_prefix}_MAX_CONNECTIONS", 100),
            command_socket_timeout_s=_env_float(f"{env_prefix}_SOCKET_TIMEOUT_S", 2.0),
            command_connect_timeout_s=_env_float(f"{env_prefix}_CONNECT_TIMEOUT_S", 3.0),
            blocking_max_connections=(
                blocking_max_connections
                if blocking_max_connections is not None
                else _env_int(f"{env_prefix}_BLOCKING_MAX_CONNECTIONS", 256)
            ),
            pool_wait_timeout_s=_env_float(f"{env_prefix}_POOL_TIMEOUT_S", 5.0),
            slow_command_ms=_env_float(f"{env_prefix}_SLOW_MS", 50.0),
        )
