"""运营库的表（c4-model/opscenter/code-accounts.html、code-tenants.html 的表结构）。

只存运营中心自己的东西：运营审计、租户操作的进度。运营人员的账号、角色、授权都在用户中心。租户名单、应用目录、份额、
系统参数都留在用它们的系统里，这里不存一份。

★ 主键约定：id 自增，只在库内用；xxx_uuid 是逻辑主键，引用和接口都用它。不建外键，引用由代码维护。
★ 时间一律 UTC。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow

__all__ = [
    "AuditEntry",
    "Base",
    "OperationStep",
    "TenantOperation",
]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _id() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


# ───────────────────────────── 运营审计


class AuditEntry(Base):
    """运营审计：只追加。detail 只放参数摘要，不放密码、临时密码、密钥。"""

    __tablename__ = "ops_audit"

    id: Mapped[int] = _id()
    audit_uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid4)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    #: 用户中心里的用户（系统租户的人）；后台任务自动发起时为空
    operator_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    #: 写入时的名字快照：账号停用后审计里照样看得懂
    operator_name: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 模块的数据编码（OpsTenant、OpsMcp……）；升级前的记录为空
    module: Mapped[str | None] = mapped_column(String(32))
    #: 对象：系统编码、租户企业代码、应用等
    target: Mapped[str | None] = mapped_column(String(200))
    tenant_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    #: OK | FAILED；跨系统的操作结束后补记
    result: Mapped[str | None] = mapped_column(String(16))
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_ops_audit_at", text("at DESC")),
        Index("ix_ops_audit_tenant", "tenant_uuid", text("at DESC")),
        Index("ix_ops_audit_module", "module", text("at DESC")),
    )


# ───────────────────────────── 租户操作


class TenantOperation(Base):
    """对一个租户的一次操作：开通、停用、恢复、关闭。步骤在 ops_operation_step。

    ★ 同一个租户同一时间只有一个进行中的操作（部分唯一索引兜底）。
    ★ 推进时抢租约（lease_owner / lease_until）：多个实例不会同时推进同一个操作。
    """

    __tablename__ = "ops_tenant_operation"

    id: Mapped[int] = _id()
    operation_uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid4)
    #: 用户中心的租户 uuid（开通时由运营中心先生成）
    tenant_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: 企业代码快照：租户删了以后记录里还看得懂
    tenant_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: OPEN | SUSPEND | RESUME | CLOSE | BACKFILL
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: RUNNING | DONE
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    #: 步骤要用的参数（开通：建租户的请求、各系统的创建参数；补建：哪个系统）。不含任何密码
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    requested_by: Mapped[UUID | None] = mapped_column(UUIDType)
    requested_by_name: Mapped[str | None] = mapped_column(String(32))
    #: 「发起」那条审计：操作结束时补记结果
    audit_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("kind IN ('OPEN','SUSPEND','RESUME','CLOSE','BACKFILL')", name="kind_enum"),
        CheckConstraint("status IN ('RUNNING','DONE')", name="status_enum"),
        Index(
            "uq_ops_tenant_operation_running",
            "tenant_uuid",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )


class OperationStep(Base):
    """操作里的一步：对哪个系统做什么。step_uuid 同时是调对方的幂等键。

    FAILED 不是终态：到了 next_attempt_at 会再做。rejected = 对方明确拒绝（前置条件不满足），
    退避更长、界面标红。
    """

    __tablename__ = "ops_operation_step"

    id: Mapped[int] = _id()
    step_uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid4)
    operation_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    system: Mapped[str] = mapped_column(String(32), nullable=False)
    #: CREATE_TENANT | BLOCK | UNBLOCK | DELETE_TENANT（用户中心）；CREATE | DELETE（各系统）。
    #: 升级前排的还可能是 SET_QUOTA | INSTALL_SKILLS | SUSPEND | RESUME | PURGE（照新契约做完）
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    #: PENDING | DONE | FAILED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    rejected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 对方返回的摘要（如删了多少条），不含任何租户内容
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("status IN ('PENDING','DONE','FAILED')", name="status_enum"),
        Index("ix_ops_operation_step_due", "status", "next_attempt_at"),
    )
