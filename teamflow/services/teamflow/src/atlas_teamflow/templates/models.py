"""流程模板领域的表：tf_flow_template、tf_flow_template_draft、tf_flow_template_version。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Audited, Base, Keys, Stamped
from ..db.types import JSONType, UTCDateTime, UUIDType, utcnow

__all__ = ["FlowTemplate", "TemplateDraft", "TemplateVersion"]


class FlowTemplate(TenantOwned, Keys, Audited, Base):
    """团队的一个流程模板：名字在租户内唯一；从流程市场拷来的记着来源。"""

    __tablename__ = "tf_flow_template"

    team_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    origin_market_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    origin_market_version: Mapped[int | None] = mapped_column(Integer)
    #: 0 = 还没发布过
    latest_version_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (
        UniqueConstraint("tenant_uuid", "name", name="uq_tf_flow_template_name"),
        CheckConstraint("status IN ('active','archived')", name="status_enum"),
    )


class TemplateDraft(TenantOwned, Keys, Base):
    """一个模板一份草稿；rev 是乐观锁。"""

    __tablename__ = "tf_flow_template_draft"

    template_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_flow_template.uuid", ondelete="CASCADE"), unique=True, nullable=False
    )
    structure: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    base_version_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rev: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    editor: Mapped[UUID | None] = mapped_column(UUIDType)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class TemplateVersion(TenantOwned, Keys, Stamped, Base):
    """发布出来的一版，之后不再改；流程发起时锁定其中一版。"""

    __tablename__ = "tf_flow_template_version"

    template_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_flow_template.uuid"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    structure: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    published_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("template_uuid", "version_no", name="uq_tf_flow_template_version"),
    )
