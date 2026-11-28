"""流程空间的表：tf_flow_space。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, Keys, Stamped
from ..db.types import UTCDateTime, UUIDType

__all__ = ["FlowSpace"]


class FlowSpace(TenantOwned, Keys, Stamped, Base):
    """一个流程空间：uuid 就是空间编号，也是 Atlas 应用工作空间的编号和幂等键
    （teamflow/{编号}/）。"""

    __tablename__ = "tf_flow_space"

    #: Atlas 申请成功的时间
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 发起成功后绑定
    process_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    #: 申请时给 Atlas 的显示名（流程标题）
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    #: applying / active / readonly / released
    status: Mapped[str] = mapped_column(String(16), default="applying", nullable=False)
    created_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    release_reason: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint(
            "status IN ('applying','active','readonly','released')", name="status_enum"
        ),
    )

    def can_write(self) -> bool:
        return self.status == "active"
