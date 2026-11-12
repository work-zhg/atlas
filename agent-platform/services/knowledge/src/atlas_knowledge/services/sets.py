"""知识集的增删改查（doc/design/knowledge-set-design.html）。

    谁能看到   维护人；其中至少有一个库能读的人；有「全部知识库」授权的人
    谁能改     维护人；「全部知识库 · Owner」
    谁能建     任何人（建的人就是维护人）
    删         只能删空的知识集（里面的库先移走或删掉）

★ 知识集只是分组，不改变权限：集里的库谁能用仍按库的可见性和数据权限。
★ 检索设置分两半：取几条（top_k）归知识集，检索方式和门槛归各个库（库的 Owner 设）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import Dataset, Document, KnowledgeSet
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..runtime import Runtime
from .cards import dataset_cards
from .permission import Visibility, visibility

__all__ = ["MAX_MAINTAINERS", "KnowledgeSetService"]

MAX_MAINTAINERS = 20
_TOP_K = (1, 20)


def _ts(v: Any) -> str | None:
    return v.isoformat() if v else None


class KnowledgeSetService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt

    # ------------------------------------------------------------------ 判定

    @staticmethod
    def _manages(ks: KnowledgeSet, p: Principal, vis: Visibility) -> bool:
        return p.auth_uid in ks.maintainers or vis.type_level == "OWNER"

    async def _visible_datasets(self, vis: Visibility) -> list[Dataset]:
        return list((await self._s.scalars(select(Dataset).where(vis.clause))).all())

    async def _load(self, set_uuid: UUID) -> KnowledgeSet:
        ks = await self._s.scalar(select(KnowledgeSet).where(KnowledgeSet.uuid == set_uuid))
        if ks is None:
            raise NotFound("知识集不存在")
        return ks

    async def manageable(
        self, set_uuid: UUID, p: Principal, *, doing: str = "往里面加知识库"
    ) -> KnowledgeSet:
        """改知识集、往里面建库 / 移库：要是它的维护人。看不到的知识集一律 404。"""
        ks = await self._load(set_uuid)
        vis = await visibility(self._rt.perms, p)
        if self._manages(ks, p, vis):
            return ks
        readable = await self._s.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.set_uuid == set_uuid, vis.clause)
        )
        if not readable:
            raise NotFound("知识集不存在")
        raise Forbidden(f"只有知识集的维护人可以{doing}")

    # ------------------------------------------------------------------ 查询

    async def list(self, p: Principal, *, keyword: str | None = None) -> list[dict[str, Any]]:
        vis = await visibility(self._rt.perms, p)
        datasets = await self._visible_datasets(vis)
        by_set: dict[UUID, list[Dataset]] = defaultdict(list)
        for d in datasets:
            by_set[d.set_uuid].append(d)
        q = select(KnowledgeSet).order_by(KnowledgeSet.created_at.desc())
        if keyword:
            q = q.where(KnowledgeSet.name.ilike(f"%{keyword.strip()}%"))
        rows = [
            ks
            for ks in (await self._s.scalars(q)).all()
            if ks.uuid in by_set or self._manages(ks, p, vis) or vis.type_level != "NONE"
        ]
        return await self._out(rows, by_set, p, vis)

    async def get(self, set_uuid: UUID, p: Principal) -> dict[str, Any]:
        ks = await self._load(set_uuid)
        vis = await visibility(self._rt.perms, p)
        datasets = list(
            (
                await self._s.scalars(
                    select(Dataset).where(Dataset.set_uuid == set_uuid, vis.clause)
                )
            ).all()
        )
        if not datasets and not self._manages(ks, p, vis) and vis.type_level == "NONE":
            raise NotFound("知识集不存在")
        return (await self._out([ks], {ks.uuid: datasets}, p, vis))[0]

    async def _out(
        self,
        rows: Sequence[KnowledgeSet],
        by_set: dict[UUID, list[Dataset]],
        p: Principal,
        vis: Visibility,
    ) -> list[dict[str, Any]]:
        names = await self._rt.perms.user_names(
            list(dict.fromkeys(u for ks in rows for u in ks.maintainers))
        )
        docs = await self._doc_counts([d.uuid for ks in rows for d in by_set.get(ks.uuid, [])])
        out = []
        for ks in rows:
            mine = by_set.get(ks.uuid, [])
            out.append(
                {
                    "id": str(ks.uuid),
                    "name": ks.name,
                    "description": ks.description,
                    "icon": ks.icon,
                    "top_k": ks.top_k,
                    "maintainers": [{"id": u, "name": names.get(u, u)} for u in ks.maintainers],
                    "created_by": ks.created_by,
                    "created_at": _ts(ks.created_at),
                    "updated_at": _ts(ks.updated_at),
                    # 只算我能读的库
                    "dataset_count": len(mine),
                    "document_count": sum(docs.get(d.uuid, 0) for d in mine),
                    "can_manage": self._manages(ks, p, vis),
                }
            )
        return out

    async def _doc_counts(self, dataset_uuids: list[UUID]) -> dict[UUID, int]:
        if not dataset_uuids:
            return {}
        # 只算当前版本：重新分段、上传新版本后，被取代的旧版本不算
        rows = await self._s.execute(
            select(Document.dataset_uuid, func.count(Document.id))
            .where(Document.dataset_uuid.in_(dataset_uuids), Document.is_current.is_(True))
            .group_by(Document.dataset_uuid)
        )
        return {u: int(c) for u, c in rows.all()}

    async def readable(self, p: Principal, set_ids: list[UUID] | None) -> list[dict[str, Any]]:
        """内部调用（智能体编辑、运行时）：auth-uid 能用的知识集，每个带上其中能读的库。

        set_ids = None 是「使用者能读的全部」。一个能读的库都没有的知识集不返回。
        """
        vis = await visibility(self._rt.perms, p)
        q = select(Dataset).where(vis.clause)
        if set_ids is not None:
            q = q.where(Dataset.set_uuid.in_(set_ids))
        datasets = (await self._s.scalars(q.order_by(Dataset.name))).all()
        by_set: dict[UUID, list[Dataset]] = defaultdict(list)
        for d in datasets:
            by_set[d.set_uuid].append(d)
        if not by_set:
            return []
        sets = (
            await self._s.scalars(
                select(KnowledgeSet)
                .where(KnowledgeSet.uuid.in_(list(by_set)))
                .order_by(KnowledgeSet.name)
            )
        ).all()
        cards = {c["id"]: c for c in await dataset_cards(self._s, datasets)}
        return [
            {
                "id": str(ks.uuid),
                "name": ks.name,
                "description": ks.description,
                "top_k": ks.top_k,
                "datasets": [cards[str(d.uuid)] for d in by_set[ks.uuid]],
            }
            for ks in sets
        ]

    # ------------------------------------------------------------------ 写

    def _fields(self, ks: KnowledgeSet, body: dict[str, Any]) -> None:
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not name:
                raise Invalid("请填写知识集名称")
            ks.name = name[:255]
        if "description" in body:
            ks.description = str(body["description"] or "").strip()
        if "icon" in body:
            ks.icon = str(body["icon"] or "📁")[:64]
        if "top_k" in body:
            try:
                top_k = int(body["top_k"])
            except (TypeError, ValueError) as exc:
                raise Invalid("取几条须是整数") from exc
            if not _TOP_K[0] <= top_k <= _TOP_K[1]:
                raise Invalid(f"取几条须在 {_TOP_K[0]} ~ {_TOP_K[1]} 之间")
            ks.top_k = top_k
        if "maintainers" in body:
            maintainers = list(dict.fromkeys(str(u) for u in body["maintainers"] or [] if u))
            if not maintainers:
                raise Invalid("知识集至少要有一个维护人")
            if len(maintainers) > MAX_MAINTAINERS:
                raise Invalid(f"维护人最多 {MAX_MAINTAINERS} 个")
            ks.maintainers = maintainers

    async def create(
        self, p: Principal, body: dict[str, Any], *, set_uuid: UUID | None = None
    ) -> KnowledgeSet:
        """任何人能建，建的人是维护人。set_uuid：建库时没指定知识集，给它配一个同 id 的。"""
        ks = KnowledgeSet(
            uuid=set_uuid or uuid4(), maintainers=[p.auth_uid], created_by=p.uid, top_k=4
        )
        self._fields(ks, {"name": body.get("name"), **body})
        self._s.add(ks)
        await self._s.flush()
        return ks

    async def update(self, set_uuid: UUID, p: Principal, body: dict[str, Any]) -> KnowledgeSet:
        ks = await self.manageable(set_uuid, p, doing="修改知识集")
        self._fields(ks, body)
        await self._s.flush()
        return ks

    async def delete(self, set_uuid: UUID, p: Principal) -> None:
        ks = await self.manageable(set_uuid, p, doing="删除知识集")
        # 看不到的库也算：库在，知识集就不能删
        left = await self._s.scalar(
            select(func.count()).select_from(Dataset).where(Dataset.set_uuid == set_uuid)
        )
        if left:
            raise Conflict(f"知识集里还有 {left} 个知识库，先移走或删掉")
        await self._s.delete(ks)
        await self._s.flush()
