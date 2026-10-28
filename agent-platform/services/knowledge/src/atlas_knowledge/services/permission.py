"""知识库的库级权限判定 —— 用户中心数据权限（数据编码 AtlasKnowledgeBase，见 perms.py）。

    读（看到、检索、召回测试）        有效级别 ≥ READ
    写（上传、编辑分段、改检索设置）  有效级别 ≥ WRITE
    管（删除、共享、换向量模型）      有效级别 = OWNER（含「全部知识库 · Owner」）
    审核入库文档                      OWNER，或 操作码 kb:review ∧ 能读（角色管「能不能审」，
                                      数据权限管「审哪些库」）

★ 一律按 auth-uid（按谁的权限判，内网身份传递 §6、§8）：人直接操作时就是本人，
  Agent 运行时是它的数字员工（P2）。只剩这一套规则。
★ 看不到的库一律 404（不暴露存在性），看得到但不能改的才 403。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import ColumnElement, false, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import Dataset
from ..errors import Forbidden, NotFound
from ..perms import LEVEL_RANK, DataPermissions

__all__ = [
    "DatasetAccess",
    "Visibility",
    "dataset_level",
    "load_dataset",
    "readable_ids",
    "visibility",
    "visible_clause",
]


async def dataset_level(perms: DataPermissions, ds: Dataset, p: Principal) -> str:
    # 没有登记到用户中心的库（存量服务账号的库，sync-uc 之前）：只有「全部知识库」授权能覆盖它
    # （check 对未登记的返回编码级）
    return await perms.level(p.auth_uid, str(ds.uuid))


async def readable_ids(
    perms: DataPermissions, subject: str | None, dataset_ids: list[UUID]
) -> set[UUID]:
    """内部调用（智能体运行时、会话 Pod 里的 MCP）：subject 能读（≥ READ）哪些库。

    ★ 绑定不是授权：每次都按这一轮的 auth-uid 问用户中心（有效级别含「全部知识库」授权），
      撤权立即生效。逐库并发问 —— 一次最多绑 20 个库。
    ★ 没有主体一个都读不到；用户中心里没有这个人（如数字员工已删除）按 NONE。
      用户中心不可用照常抛 Unavailable：不放行。
    """
    if not subject or not dataset_ids:
        return set()

    async def level(dataset_id: UUID) -> str:
        try:
            return await perms.level(subject, str(dataset_id))
        except NotFound:
            return "NONE"

    levels = await asyncio.gather(*(level(d) for d in dataset_ids))
    return {
        d
        for d, lv in zip(dataset_ids, levels, strict=True)
        if LEVEL_RANK.get(lv, 0) >= LEVEL_RANK["READ"]
    }


class Visibility:
    """一次查询用户中心，得到列表过滤条件与每个库的级别（列表页要显示「我的权限」）。"""

    def __init__(self, p: Principal, levels: dict[str, str], type_level: str) -> None:
        self.p = p
        self._levels = levels
        self._type = type_level

    @property
    def clause(self) -> ColumnElement[bool]:
        if LEVEL_RANK[self._type] >= LEVEL_RANK["READ"]:
            return true()
        readable = [UUID(i) for i in self._levels]
        return Dataset.uuid.in_(readable) if readable else false()

    def level(self, ds: Dataset) -> str:
        own = self._levels.get(str(ds.uuid), "NONE")
        return own if LEVEL_RANK[own] >= LEVEL_RANK[self._type] else self._type


async def visibility(perms: DataPermissions, p: Principal) -> Visibility:
    """列表：用户中心里可读的库；有「全部知识库」授权则全部。"""
    levels, type_level = await perms.accessible(p.auth_uid, "READ")
    return Visibility(p, levels, type_level)


async def visible_clause(perms: DataPermissions, p: Principal) -> ColumnElement[bool]:
    return (await visibility(perms, p)).clause


class DatasetAccess:
    def __init__(self, ds: Dataset, p: Principal, level: str) -> None:
        self.ds = ds
        self.p = p
        self.level = level

    @property
    def is_admin(self) -> bool:
        return self.level == "OWNER"

    @property
    def can_read(self) -> bool:
        return LEVEL_RANK[self.level] >= LEVEL_RANK["READ"]

    @property
    def can_write(self) -> bool:
        return LEVEL_RANK[self.level] >= LEVEL_RANK["WRITE"]

    @property
    def can_review(self) -> bool:
        return self.is_admin or (self.p.has("kb:review") and self.can_read)

    @property
    def shareable(self) -> bool:
        """存量服务账号的库（sync-uc 之前）没有登记到用户中心，不能共享。"""
        return self.ds.owner_kind == "user"

    def require_write(self) -> None:
        if not self.can_write:
            raise Forbidden("没有修改该知识库的权限")

    def require_admin(self) -> None:
        if not self.is_admin:
            raise Forbidden("只有知识库所有者可以执行该操作")

    def require_review(self) -> None:
        if not self.can_review:
            raise Forbidden("没有审核该知识库文档的权限")


async def load_dataset(
    session: AsyncSession, perms: DataPermissions, dataset_uuid: UUID, p: Principal
) -> DatasetAccess:
    ds = await session.scalar(select(Dataset).where(Dataset.uuid == dataset_uuid))
    if ds is None:
        raise NotFound("知识库不存在")
    access = DatasetAccess(ds, p, await dataset_level(perms, ds, p))
    if not access.can_read:
        raise NotFound("知识库不存在")
    return access
