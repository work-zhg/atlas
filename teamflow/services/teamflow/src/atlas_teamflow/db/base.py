"""表的公共部分：DeclarativeBase、主键约定、时间戳。各领域的表在自己的包里（teams/models.py …）。

主键约定：`id` 自增主键只在库内使用；`uuid` 逻辑主键（Python 侧生成 uuid7），表间引用的列叫
xxx_uuid，对外（接口、链接）一律用 uuid。
★ 业务表都继承 atlas_tenancy.TenantOwned（多一列 tenant_uuid，查询按当前租户自动过滤）；
  流程市场的表和通知器的游标不属于任何租户，不继承。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Identity, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..ids import uuid7
from .types import UTCDateTime, UUIDType, utcnow

__all__ = ["Audited", "Base", "Keys", "Stamped"]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Keys:
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False, default=uuid7)


class Stamped:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Audited(Stamped):
    """可改的表：谁最后改的、什么时候，加乐观锁版本。"""

    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    updated_by: Mapped[UUID | None] = mapped_column(UUIDType)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
