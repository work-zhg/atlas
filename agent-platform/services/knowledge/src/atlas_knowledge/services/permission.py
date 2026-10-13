"""知识库的权限判定（知识库设计 §11.2）。

可见范围同 Dify：only_me / all_team / partial_members
（成员表 kb_dataset_permission，read | write）。

    读（看到、检索、召回测试）  owner、kb:manage_all、all_team、partial 成员
    写（上传、编辑分段、改检索设置）owner、kb:manage_all、partial 成员中 level=write
    管（删除、改可见范围）        owner、kb:manage_all

★ 先落在本服务的成员表上；Atlas 权限 P2（用户中心数据权限）接入后，成员表换成数据权限查询，
  判定函数的签名不变。
★ 看不到的库一律 404（不暴露存在性），看得到但不能改的才 403。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import Dataset, DatasetPermission
from ..errors import Forbidden, NotFound

__all__ = ["DatasetAccess", "load_dataset", "visible_clause"]


def visible_clause(p: Principal) -> ColumnElement[bool]:
    if p.has("kb:manage_all"):
        return Dataset.id.is_not(None)
    members = select(DatasetPermission.dataset_uuid).where(DatasetPermission.principal_id == p.id)
    return or_(
        Dataset.owner_id == p.id,
        Dataset.permission == "all_team",
        and_(Dataset.permission == "partial_members", Dataset.uuid.in_(members)),
    )


class DatasetAccess:
    def __init__(self, ds: Dataset, p: Principal, member_level: str | None) -> None:
        self.ds = ds
        self.p = p
        self._level = member_level

    @property
    def is_admin(self) -> bool:
        return self.p.has("kb:manage_all") or self.ds.owner_id == self.p.id

    @property
    def can_read(self) -> bool:
        if self.is_admin or self.ds.permission == "all_team":
            return True
        return self.ds.permission == "partial_members" and self._level is not None

    @property
    def can_write(self) -> bool:
        if self.is_admin:
            return True
        return self.ds.permission == "partial_members" and self._level == "write"

    @property
    def can_review(self) -> bool:
        return self.is_admin or self.p.has("kb:review")

    def require_write(self) -> None:
        if not self.can_write:
            raise Forbidden("没有修改该知识库的权限")

    def require_admin(self) -> None:
        if not self.is_admin:
            raise Forbidden("只有知识库所有者或管理员可以执行该操作")

    def require_review(self) -> None:
        if not self.can_review:
            raise Forbidden("没有审核该知识库文档的权限")


async def load_dataset(session: AsyncSession, dataset_uuid: UUID, p: Principal) -> DatasetAccess:
    ds = await session.scalar(select(Dataset).where(Dataset.uuid == dataset_uuid))
    if ds is None:
        raise NotFound("知识库不存在")
    level = await session.scalar(
        select(DatasetPermission.level).where(
            DatasetPermission.dataset_uuid == dataset_uuid,
            DatasetPermission.principal_id == p.id,
        )
    )
    access = DatasetAccess(ds, p, level)
    if not access.can_read:
        raise NotFound("知识库不存在")
    return access
