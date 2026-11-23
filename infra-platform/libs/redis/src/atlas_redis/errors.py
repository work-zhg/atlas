"""Redis 中间件的异常体系（c4-model/infra-platform/code-redis.html）。

★ 明确区分三类失败：
  · 护栏拦截（CommandForbidden / PayloadTooLarge）：调用方代码误用，不应重试、不应触发熔断；
  · 基础设施不可用（RedisUnavailable / CircuitOpen）：连接超时、断连或熔断器开启，由原语按
    Fail-Closed / Fail-Open 分别处理；
  · 脚本缺失（由 ScriptRegistry 内部捕获并自动回退 EVAL，不向外泄漏）。
"""

from __future__ import annotations

__all__ = [
    "CircuitOpen",
    "CommandForbidden",
    "PayloadTooLarge",
    "RedisMiddlewareError",
    "RedisUnavailable",
]


class RedisMiddlewareError(Exception):
    """atlas_redis 所有显式异常的基类。"""


class CommandForbidden(RedisMiddlewareError):
    """尝试执行被 GuardMiddleware 禁止的危险命令（如 KEYS / FLUSHALL）。"""

    def __init__(self, command: str) -> None:
        super().__init__(f"禁止执行危险 Redis 命令：{command}")
        self.command = command


class PayloadTooLarge(RedisMiddlewareError):
    """单次命令或管线写入载荷超过 GuardMiddleware 配置的硬上限。"""

    def __init__(self, command: str, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"Redis 写入载荷超限：{command} 为 {size_bytes} 字节，超过上限 {max_bytes} 字节"
        )
        self.command = command
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


class RedisUnavailable(RedisMiddlewareError):
    """Redis 连接失败或命令超时（仅在 Fail-Closed 路径抛出）。"""


class CircuitOpen(RedisUnavailable):
    """熔断器处于开启状态，请求被快速拒绝以避免雪崩。"""

    def __init__(self, service: str, retry_after_s: float) -> None:
        super().__init__(f"Redis 熔断器开启（service={service}），约 {retry_after_s:.1f}s 后重试")
        self.service = service
        self.retry_after_s = retry_after_s
