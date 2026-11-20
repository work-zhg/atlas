"""租户上下文：这一次读写是替哪个租户做的（c4-model/infra-platform/code-tenancy.html）。

    TenantScope.enter(tenant)     进入一个租户：之后本协程（及它派生的子任务）的查询、写入、
                                  出站请求都属于这个租户
    TenantScope.system(reason)    显式进入系统范围（不过滤），只有 SWEEP、OPS 两种原因
    TenantScope.bind(tenant)      请求入口用：设定后不退出，随请求所在的任务一起结束

★ 放在 contextvar 里而不是数据库会话上：一个请求、一个 run 里会开好几个会话，还要调下游、
  拼对象存储路径；asyncio 子任务创建时复制上下文，并行的工具调用、子智能体自动继承同一个租户。
★ 没有上下文 ≠ 系统范围：没有上下文时碰租户表直接报错（见 filter.py），系统范围必须显式进入。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from uuid import UUID

__all__ = ["SystemReason", "TenantScope", "TenantViolation"]

logger = logging.getLogger(__name__)


class SystemReason(StrEnum):
    """进入系统范围的原因。只有这三个；加新的要过评审。"""

    #: 巡检、兜底：只取待办的 id 和租户，处理时逐条进入租户（TenantSweep）
    SWEEP = "sweep"
    #: 运营接口：用量统计、份额、删除租户数据
    OPS = "ops"
    #: 凭据换身份：按全局唯一的凭据（设备令牌、会话令牌）查出它属于哪个租户，只查那一行，
    #: 查到就进入它的租户。还不知道租户时只能这样（用户中心解析令牌也是同一个道理）
    CREDENTIAL = "credential"


class TenantViolation(RuntimeError):
    """代码问题，不是用户错误：没有租户上下文就碰租户表、在 A 的上下文里写 B 的行。"""

    MISSING = "missing"
    CROSS_TENANT = "cross_tenant"

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


_scope: ContextVar[UUID | SystemReason | None] = ContextVar("atlas_tenant_scope", default=None)


class TenantScope:
    """租户上下文的唯一入口。都是类方法：上下文本身在 contextvar 里，不需要实例。"""

    @staticmethod
    def current() -> UUID | None:
        """当前租户；系统范围或没有上下文时为 None。"""
        value = _scope.get()
        return value if isinstance(value, UUID) else None

    @staticmethod
    def system_reason() -> SystemReason | None:
        value = _scope.get()
        return value if isinstance(value, SystemReason) else None

    @staticmethod
    def require() -> UUID:
        """当前租户；不在某个租户里就是代码问题。"""
        value = _scope.get()
        if not isinstance(value, UUID):
            raise TenantViolation(
                TenantViolation.MISSING, "这里需要租户上下文，但当前不在任何租户里"
            )
        return value

    @staticmethod
    @contextmanager
    def enter(tenant_uuid: UUID) -> Iterator[UUID]:
        if not isinstance(tenant_uuid, UUID):
            raise TypeError(f"租户必须是 UUID，收到 {type(tenant_uuid).__name__}")
        token = _scope.set(tenant_uuid)
        try:
            yield tenant_uuid
        finally:
            _scope.reset(token)

    @staticmethod
    @contextmanager
    def system(reason: SystemReason) -> Iterator[None]:
        if not isinstance(reason, SystemReason):
            raise TypeError("进入系统范围必须给出 SystemReason")
        logger.debug("进入系统范围：%s", reason)
        token = _scope.set(reason)
        try:
            yield
        finally:
            _scope.reset(token)

    @staticmethod
    @contextmanager
    def isolated() -> Iterator[None]:
        """一段干净的上下文：进入时清空，退出时还原。

        给请求中间件、应用的 lifespan 用：同一个任务里跑多个请求（测试的 ASGITransport）时
        上一个请求的租户不会串到下一个；lifespan 里起的后台任务不会继承启动方的租户。
        """
        token = _scope.set(None)
        try:
            yield
        finally:
            _scope.reset(token)

    @staticmethod
    def bind(tenant_uuid: UUID) -> None:
        """设定后不退出：只用在请求入口，上下文随请求所在的任务一起结束。

        不用 enter 是因为 FastAPI 的 yield 依赖可能在别的上下文里收尾，reset 会报错。
        """
        if not isinstance(tenant_uuid, UUID):
            raise TypeError(f"租户必须是 UUID，收到 {type(tenant_uuid).__name__}")
        _scope.set(tenant_uuid)


def raw_scope() -> UUID | SystemReason | None:
    """给 filter.py 用：原样的上下文值。"""
    return _scope.get()
