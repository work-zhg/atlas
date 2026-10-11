"""分段与子块管理（同 Dify SegmentService）。

★ 单个分段的增改删是同步做的（向量化一段只要几十毫秒）；Dify 走异步任务，但对单段来说
  同步更简单，用户改完立刻能在召回测试里看到效果。
★ 父子模式：改父分段内容会按文档的子块规则重新切子块；子块也可以单独增删改。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ChildChunk, Document, ProcessRule, Segment
from ..errors import Conflict, Invalid, NotFound
from ..indexing.loader import index_segments, point_ids_of, remove_segments
from ..rag.index_processor import HIERARCHICAL_DEFAULT_RULES
from ..rag.splitter import FixedRecursiveCharacterTextSplitter
from ..runtime import Runtime
from .permission import DatasetAccess

__all__ = ["SegmentService"]


class SegmentService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt

    def _require_indexed(self, doc: Document) -> None:
        if doc.indexing_status != "completed":
            raise Conflict("文档入库完成后才能编辑分段")

    async def get(self, doc: Document, segment_uuid: UUID) -> Segment:
        seg = await self._s.scalar(
            select(Segment).where(Segment.uuid == segment_uuid, Segment.document_uuid == doc.uuid)
        )
        if seg is None:
            raise NotFound("分段不存在")
        return seg

    async def list(
        self,
        doc: Document,
        *,
        page: int = 1,
        limit: int = 20,
        keyword: str | None = None,
        enabled: bool | None = None,
    ) -> tuple[list[Segment], int, dict[str, list[ChildChunk]]]:
        conds: list[Any] = [Segment.document_uuid == doc.uuid]
        if keyword:
            like = f"%{keyword.strip()}%"
            conds.append(or_(Segment.content.ilike(like), Segment.answer.ilike(like)))
        if enabled is not None:
            conds.append(Segment.enabled.is_(enabled))
        total = int(
            await self._s.scalar(select(func.count()).select_from(Segment).where(*conds)) or 0
        )
        segs = list(
            (
                await self._s.scalars(
                    select(Segment)
                    .where(*conds)
                    .order_by(Segment.position)
                    .offset((page - 1) * limit)
                    .limit(limit)
                )
            ).all()
        )
        children: dict[str, list[ChildChunk]] = {}
        if doc.doc_form == "hierarchical_model" and segs:
            rows = await self._s.scalars(
                select(ChildChunk)
                .where(ChildChunk.segment_uuid.in_([s.uuid for s in segs]))
                .order_by(ChildChunk.position)
            )
            for c in rows.all():
                children.setdefault(str(c.segment_uuid), []).append(c)
        return segs, total, children

    async def children(self, seg: Segment) -> list[ChildChunk]:
        rows = await self._s.scalars(
            select(ChildChunk)
            .where(ChildChunk.segment_uuid == seg.uuid)
            .order_by(ChildChunk.position)
        )
        return list(rows.all())

    # ------------------------------------------------------------------ 分段

    async def _child_splitter(self, doc: Document) -> FixedRecursiveCharacterTextSplitter:
        rule = await self._s.scalar(
            select(ProcessRule).where(ProcessRule.uuid == doc.process_rule_uuid)
        )
        seg = (rule.rules if rule else HIERARCHICAL_DEFAULT_RULES).get(
            "subchunk_segmentation"
        ) or HIERARCHICAL_DEFAULT_RULES["subchunk_segmentation"]
        return FixedRecursiveCharacterTextSplitter(
            chunk_size=seg["max_tokens"],
            chunk_overlap=seg["chunk_overlap"],
            fixed_separator=seg["separator"],
        )

    async def _rebuild_children(self, doc: Document, seg: Segment, actor: str) -> None:
        old = await self.children(seg)
        ids = [c.index_node_id for c in old if c.index_node_id]
        ds_collection = await self._collection(doc)
        await self._rt.vectors.for_collection(ds_collection).delete_by_ids(ids)
        for c in old:
            await self._s.delete(c)
        splitter = await self._child_splitter(doc)
        for pos, text in enumerate((t.strip() for t in splitter.split_text(seg.content)), start=1):
            if text:
                self._s.add(
                    ChildChunk(
                        dataset_uuid=seg.dataset_uuid,
                        document_uuid=doc.uuid,
                        segment_uuid=seg.uuid,
                        position=pos,
                        content=text,
                        word_count=len(text),
                        created_by=actor,
                    )
                )
        await self._s.flush()

    async def _collection(self, doc: Document) -> str:
        from ..db.models import Dataset

        name = await self._s.scalar(
            select(Dataset.collection_name).where(Dataset.uuid == doc.dataset_uuid)
        )
        assert name is not None
        return name

    async def create(self, access: DatasetAccess, doc: Document, content: str) -> Segment:
        access.require_write()
        self._require_indexed(doc)
        content = content.strip()
        if not content:
            raise Invalid("分段内容不能为空")
        pos = int(
            await self._s.scalar(
                select(func.coalesce(func.max(Segment.position), 0)).where(
                    Segment.document_uuid == doc.uuid
                )
            )
            or 0
        )
        seg = Segment(
            dataset_uuid=doc.dataset_uuid,
            document_uuid=doc.uuid,
            position=pos + 1,
            content=content,
            word_count=len(content),
            status="indexing",
            created_by=access.p.id,
        )
        self._s.add(seg)
        await self._s.flush()
        if doc.doc_form == "hierarchical_model":
            await self._rebuild_children(doc, seg, access.p.id)
        await index_segments(self._s, self._rt, access.ds, doc, [seg])
        doc.word_count += seg.word_count
        return seg

    async def update(
        self,
        access: DatasetAccess,
        doc: Document,
        seg: Segment,
        *,
        content: str | None = None,
        enabled: bool | None = None,
    ) -> Segment:
        access.require_write()
        self._require_indexed(doc)
        if content is not None and content.strip() != seg.content:
            content = content.strip()
            if not content:
                raise Invalid("分段内容不能为空")
            doc.word_count += len(content) - seg.word_count
            seg.content = content
            seg.word_count = len(content)
            seg.updated_by = access.p.id
            if doc.doc_form == "hierarchical_model":
                await self._rebuild_children(doc, seg, access.p.id)
            await index_segments(self._s, self._rt, access.ds, doc, [seg])
        if enabled is not None and enabled != seg.enabled:
            seg.enabled = enabled
            seg.updated_by = access.p.id
            ids = await point_ids_of(self._s, [seg])
            await self._rt.vectors.for_collection(access.ds.collection_name).set_enabled(
                ids, enabled
            )
        await self._s.flush()
        return seg

    async def delete(self, access: DatasetAccess, doc: Document, seg: Segment) -> None:
        access.require_write()
        doc.word_count = max(0, doc.word_count - seg.word_count)
        await remove_segments(self._s, self._rt, access.ds, [seg])
        await self._s.flush()

    # ------------------------------------------------------------------ 子块

    def _require_hierarchical(self, doc: Document) -> None:
        if doc.doc_form != "hierarchical_model":
            raise Invalid("只有父子分段的文档有子块")

    async def _reindex_children(self, access: DatasetAccess, doc: Document, seg: Segment) -> None:
        await self._s.flush()
        await index_segments(self._s, self._rt, access.ds, doc, [seg])

    async def add_child(
        self, access: DatasetAccess, doc: Document, seg: Segment, content: str
    ) -> ChildChunk:
        access.require_write()
        self._require_hierarchical(doc)
        self._require_indexed(doc)
        content = content.strip()
        if not content:
            raise Invalid("子块内容不能为空")
        pos = int(
            await self._s.scalar(
                select(func.coalesce(func.max(ChildChunk.position), 0)).where(
                    ChildChunk.segment_uuid == seg.uuid
                )
            )
            or 0
        )
        child = ChildChunk(
            dataset_uuid=seg.dataset_uuid,
            document_uuid=doc.uuid,
            segment_uuid=seg.uuid,
            position=pos + 1,
            content=content,
            word_count=len(content),
            type="customized",
            created_by=access.p.id,
        )
        self._s.add(child)
        await self._reindex_children(access, doc, seg)
        return child

    async def _child(self, seg: Segment, child_uuid: UUID) -> ChildChunk:
        child = await self._s.scalar(
            select(ChildChunk).where(
                ChildChunk.uuid == child_uuid, ChildChunk.segment_uuid == seg.uuid
            )
        )
        if child is None:
            raise NotFound("子块不存在")
        return child

    async def update_child(
        self, access: DatasetAccess, doc: Document, seg: Segment, child_uuid: UUID, content: str
    ) -> ChildChunk:
        access.require_write()
        self._require_hierarchical(doc)
        child = await self._child(seg, child_uuid)
        content = content.strip()
        if not content:
            raise Invalid("子块内容不能为空")
        child.content = content
        child.word_count = len(content)
        child.type = "customized"
        await self._reindex_children(access, doc, seg)
        return child

    async def delete_child(
        self, access: DatasetAccess, doc: Document, seg: Segment, child_uuid: UUID
    ) -> None:
        access.require_write()
        self._require_hierarchical(doc)
        child = await self._child(seg, child_uuid)
        if child.index_node_id:
            await self._rt.vectors.for_collection(access.ds.collection_name).delete_by_ids(
                [child.index_node_id]
            )
        await self._s.delete(child)
        await self._s.flush()
