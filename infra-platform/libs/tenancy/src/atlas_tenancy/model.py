"""租户表：继承 TenantOwned 就多一列 tenant_uuid（非空、建索引），查询自动按当前租户过滤。

★ 每张业务表都继承，包括子表（运行事件、分段……）：自动过滤只看得到当前查的这张表，
  后台按 id 直接查子数据时也要受保护。
★ 列没有默认值：新行由 filter.py 在 flush 前填上当前租户；ORM 批量 INSERT 不经过 flush，
  要自己写 tenant_uuid，漏写会被 NOT NULL 拦住。
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

__all__ = ["TenantOwned"]


class TenantOwned:
    """mixin：放在宿主自己的 DeclarativeBase 前面，class Agent(TenantOwned, Base)。"""

    @declared_attr
    def tenant_uuid(cls) -> Mapped[UUID]:
        return mapped_column(sa.Uuid, nullable=False, index=True)
