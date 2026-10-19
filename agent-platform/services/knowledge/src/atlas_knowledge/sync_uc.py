"""一次性迁移：把接入用户中心之前的库级权限写进用户中心（数据编码 AtlasKnowledgeBase）。

    python -m atlas_knowledge sync-uc [--service-owner <用户中心用户 id>]

对每个 owner_kind = user 的库：
  · 用户中心里已经有授权的 → 跳过（可以重复执行）
  · 所有者写成 OWNER；所有者在用户中心里不存在 → 认定是服务账号建的库，改为 owner_kind = service
  · 旧可见范围 all_team → 根部门（含下级）只读；partial_members 的成员 → 只读 / 读写
    （单个成员在用户中心里不存在的跳过并记日志，不影响其余）

去服务账号（内网身份传递 §14）：给了 --service-owner 时，owner_kind = service 的库（服务账号
建的，没有登记到用户中心）登记到用户中心，OWNER = 这个人（如评测用户 atlas-bench），
改为 owner_kind = user。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Dataset, DatasetPermission
from .errors import Invalid, NotFound
from .perms import DataPermissions, Grant

logger = logging.getLogger(__name__)

__all__ = ["sync_uc"]

_LEVEL = {"read": "READ", "write": "WRITE"}


async def sync_uc(
    session: AsyncSession, perms: DataPermissions, *, service_owner: str | None = None
) -> dict[str, int]:
    out = {"synced": 0, "skipped": 0, "service": 0, "member_failed": 0, "adopted": 0}
    rows = (await session.scalars(select(Dataset).where(Dataset.owner_kind == "user"))).all()
    root: str | None = None
    for ds in rows:
        data_id = str(ds.uuid)
        if await perms.acl(data_id):
            out["skipped"] += 1
            continue
        try:
            await perms.create(data_id, ds.name, [Grant("OWNER", "user", ds.owner_id)])
        except (Invalid, NotFound) as exc:
            # ★ 只认「所有者不在用户中心」；用户中心不可达（Unavailable）直接中止，不能误判
            logger.warning(
                "库 %s 的所有者 %s 不在用户中心（%s），改为服务账号的库",
                ds.name,
                ds.owner_id,
                exc.message,
            )
            ds.owner_kind = "service"
            out["service"] += 1
            continue
        extra: list[Grant] = []
        if ds.permission == "all_team":
            root = root or await perms.root_dept()
            extra.append(Grant("READ", "dept", root, True))
        elif ds.permission == "partial_members":
            members: Any = await session.scalars(
                select(DatasetPermission).where(DatasetPermission.dataset_uuid == ds.uuid)
            )
            extra += [
                Grant(_LEVEL.get(m.level, "READ"), "user", m.principal_id)
                for m in members
                if m.principal_id != ds.owner_id
            ]
        for g in extra:
            try:
                await perms.grant(data_id, ds.owner_id, [g])
            except (Invalid, NotFound) as exc:
                logger.warning("库 %s 的成员 %s 写入失败：%s", ds.name, g.subject_id, exc.message)
                out["member_failed"] += 1
        out["synced"] += 1
    if service_owner:
        service = (
            await session.scalars(select(Dataset).where(Dataset.owner_kind == "service"))
        ).all()
        for ds in service:
            if not await perms.acl(str(ds.uuid)):
                await perms.create(str(ds.uuid), ds.name, [Grant("OWNER", "user", service_owner)])
            ds.owner_kind, ds.owner_id = "user", service_owner
            out["adopted"] += 1
    await session.commit()
    return out
