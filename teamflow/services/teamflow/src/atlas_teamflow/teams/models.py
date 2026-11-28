"""团队领域的表：tf_team、tf_team_agent。成员和级别不在这里，在用户中心的「团队」数据权限里。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Audited, Base, Keys, Stamped
from ..db.types import UTCDateTime, UUIDType, utcnow

__all__ = ["Team", "TeamAgent"]


class Team(TenantOwned, Keys, Audited, Base):
    """一个团队：名字在租户内唯一；归档后不能再发起流程，进行中的跑完。"""

    __tablename__ = "tf_team"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (
        UniqueConstraint("tenant_uuid", "name", name="uq_tf_team_name"),
        CheckConstraint("status IN ('active','archived')", name="status_enum"),
    )


class TeamAgent(TenantOwned, Keys, Stamped, Base):
    """团队接入的数字员工：Atlas 智能体的引用 + 显示用的快照。接入人是它的 Owner，TeamFlow
    以接入人的身份给流程里的人授使用权。"""

    __tablename__ = "tf_team_agent"

    team_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_team.uuid"), nullable=False)
    atlas_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: native / cli（快照）
    kind: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    #: 接入人（接入时是这个数字员工的 Owner），授使用权时的操作人
    added_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)

    __table_args__ = (UniqueConstraint("team_uuid", "atlas_agent_id", name="uq_tf_team_agent"),)
