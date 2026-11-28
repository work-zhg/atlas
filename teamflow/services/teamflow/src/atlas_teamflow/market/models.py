"""流程市场的表：tf_market_template、tf_market_version、tf_market_copy。

★ 不属于任何租户：不继承 TenantOwned，不经租户自动过滤，所有租户看到同一份。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Audited, Base, Keys, Stamped
from ..db.types import JSONType, UTCDateTime, UUIDType, utcnow

__all__ = ["CATEGORIES", "MarketCopy", "MarketTemplate", "MarketVersion"]

#: 分类：左侧二级菜单按它分
CATEGORIES = {"rd": "软件研发", "content": "内容", "legal": "法务与合规", "data": "数据分析"}


class MarketTemplate(Keys, Audited, Base):
    """市场里的一个模板：草稿在自己身上，发布成版本；第一次发布即上架，之后可以下架、再上架。"""

    __tablename__ = "tf_market_template"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 谁把关（详情页显示）
    who: Mapped[str | None] = mapped_column(String(100))
    icon: Mapped[str | None] = mapped_column(String(16))
    color: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    draft: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: 0 = 还没发布过
    latest_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (
        CheckConstraint("status IN ('draft','listed','unlisted')", name="status_enum"),
        CheckConstraint("category IN ('rd','content','legal','data')", name="category_enum"),
    )


class MarketVersion(Keys, Stamped, Base):
    __tablename__ = "tf_market_version"

    market_template_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_market_template.uuid"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    structure: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    published_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("market_template_uuid", "version", name="uq_tf_market_version"),
    )


class MarketCopy(Keys, Stamped, Base):
    """一次拷贝：哪个模板的哪一版拷到了哪个租户的哪个团队。只用来统计。"""

    __tablename__ = "tf_market_copy"

    market_template_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_market_template.uuid"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 普通列（这张表不按租户过滤）
    tenant_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    team_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    copied_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)

    __table_args__ = (Index("ix_tf_market_copy_template", "market_template_uuid", "tenant_uuid"),)
