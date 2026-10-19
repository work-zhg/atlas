"""文档（同 Dify DocumentService）：上传、分段预览、状态操作、审核、删除。

★ 入库任务在**事务提交之后**才投递：否则 worker 可能比提交更早读库，读不到文档。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ChildChunk, Document, ProcessRule, Segment
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from ..indexing.loader import sync_doc_payload
from ..indexing.runner import pause_key
from ..indexing.versions import stores
from ..rag import index_processor
from ..rag.extractor import extension_of
from ..rag.parsers import SUPPORTED_EXTENSIONS, parse
from ..runtime import Runtime
from .permission import DatasetAccess

__all__ = ["DocumentService", "UploadFile"]

logger = logging.getLogger(__name__)

_RUNNING = ("waiting", "parsing", "splitting", "indexing")


@dataclass
class UploadFile:
    filename: str
    data: bytes
    content_type: str | None = None


class DocumentService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt

    # ------------------------------------------------------------------ 查询

    async def get(self, access: DatasetAccess, document_uuid: UUID) -> Document:
        doc = await self._s.scalar(
            select(Document).where(
                Document.uuid == document_uuid, Document.dataset_uuid == access.ds.uuid
            )
        )
        if doc is None:
            raise NotFound("文档不存在")
        return doc

    async def list(
        self,
        access: DatasetAccess,
        *,
        page: int = 1,
        limit: int = 20,
        keyword: str | None = None,
        status: str | None = None,
    ) -> tuple[list[Document], int]:
        conds: list[Any] = [Document.dataset_uuid == access.ds.uuid]
        if keyword:
            conds.append(Document.name.ilike(f"%{keyword.strip()}%"))
        if status == "available":
            conds += [Document.enabled.is_(True), Document.archived.is_(False)]
        elif status == "disabled":
            conds.append(Document.enabled.is_(False))
        elif status == "archived":
            conds.append(Document.archived.is_(True))
        elif status == "pending_review":
            conds.append(Document.review_status == "pending")
        elif status:
            conds.append(Document.indexing_status == status)
        total = int(
            await self._s.scalar(select(func.count()).select_from(Document).where(*conds)) or 0
        )
        rows = await self._s.scalars(
            select(Document)
            .where(*conds)
            .order_by(Document.position.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        return list(rows.all()), total

    async def segment_counts(self, document_uuids: list[UUID]) -> dict[UUID, tuple[int, int]]:
        """文档 → (分段总数, 已完成数)。"""
        if not document_uuids:
            return {}
        rows = await self._s.execute(
            select(
                Segment.document_uuid,
                func.count(Segment.id),
                func.count(Segment.id).filter(Segment.status == "completed"),
            )
            .where(Segment.document_uuid.in_(document_uuids))
            .group_by(Segment.document_uuid)
        )
        return {u: (int(t), int(c)) for u, t, c in rows.all()}

    async def batch(self, access: DatasetAccess, batch: str) -> list[Document]:
        rows = await self._s.scalars(
            select(Document)
            .where(Document.dataset_uuid == access.ds.uuid, Document.batch == batch)
            .order_by(Document.position)
        )
        return list(rows.all())

    async def process_rule(self, rule_uuid: UUID) -> ProcessRule | None:
        return await self._s.scalar(select(ProcessRule).where(ProcessRule.uuid == rule_uuid))

    # ------------------------------------------------------------------ 预览

    def _check_file(self, f: UploadFile) -> str:
        ext = extension_of(f.filename)
        if ext not in SUPPORTED_EXTENSIONS:
            raise Invalid(f"不支持的文件类型：.{ext}", supported=sorted(SUPPORTED_EXTENSIONS))
        if len(f.data) > self._rt.settings.upload_max_bytes:
            raise Invalid(
                f"文件「{f.filename}」超过 {self._rt.settings.upload_max_bytes // 1024 // 1024}MB"
            )
        if not f.data:
            raise Invalid(f"文件「{f.filename}」是空的")
        return ext

    def _rule(
        self, doc_form: str, process_rule: dict[str, Any] | None
    ) -> tuple[str, dict[str, Any]]:
        process_rule = process_rule or {}
        default_mode = "hierarchical" if doc_form == "hierarchical_model" else "automatic"
        return index_processor.normalize_process_rule(
            str(process_rule.get("mode") or default_mode),
            process_rule.get("rules"),
            doc_form=doc_form,
            max_length=self._rt.settings.max_segmentation_length,
        )

    async def estimate(
        self, f: UploadFile, *, doc_form: str, process_rule: dict[str, Any] | None
    ) -> dict[str, Any]:
        """分段预览（同 Dify indexing-estimate）：返回前 10 段与总数。"""
        self._check_file(f)
        _, rules = self._rule(doc_form, process_rule)
        ir = await asyncio.to_thread(parse, f.filename, f.data, rules.get("parser") or "auto")
        embed = None
        if (rules.get("structure") or {}).get("semantic_split"):
            # 预览不属于某个知识库：语义分段用默认向量模型（与入库时可能略有差异）
            model = await asyncio.to_thread(
                self._rt.registry.embedding, "fastembed", self._rt.settings.embedding_model
            )
            embed = model.embed_documents
        nodes = await asyncio.to_thread(
            index_processor.transform_ir,
            ir,
            doc_form=doc_form,
            rules=rules,
            embed=embed,
            doc_title=index_processor.doc_title_of(ir, f.filename),
        )
        preview = []
        for n in nodes[:10]:
            item: dict[str, Any] = {
                "content": n.page_content,
                "word_count": len(n.page_content),
                "title_path": n.metadata.get("title_path") or [],
            }
            if n.children is not None:
                item["child_chunks"] = [c.page_content for c in n.children[:20]]
                item["child_count"] = len(n.children)
            preview.append(item)
        return {
            "total_segments": len(nodes),
            "total_words": sum(len(n.page_content) for n in nodes),
            "preview": preview,
            "rules": rules,
        }

    # ------------------------------------------------------------------ 上传

    async def upload(
        self,
        access: DatasetAccess,
        files: list[UploadFile],
        *,
        process_rule: dict[str, Any] | None,
        doc_form: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> tuple[str, list[Document]]:
        """attrs：文档属性（metadata / acl / valid_from / valid_to / supersedes），
        批内各文档相同。"""
        from .metadata import MetadataService, _when, normalize_acl

        access.require_write()
        ds = access.ds
        if not files:
            raise Invalid("请选择要上传的文件")
        attrs = attrs or {}
        meta_svc = MetadataService(self._s, self._rt)
        doc_metadata = await meta_svc.normalize_metadata(access, attrs.get("metadata"))
        acl = normalize_acl(attrs.get("acl"))
        valid_from = _when(attrs.get("valid_from"), "valid_from")
        valid_to = _when(attrs.get("valid_to"), "valid_to")
        supersedes = attrs.get("supersedes")
        if supersedes and len(files) != 1:
            raise Invalid("上传新版本时一次只能传一个文件")
        doc_form = doc_form or ds.doc_form
        count = int(
            await self._s.scalar(
                select(func.count()).select_from(Document).where(Document.dataset_uuid == ds.uuid)
            )
            or 0
        )
        if doc_form != ds.doc_form:
            if count:
                raise Invalid("同一知识库内的文档须使用相同的分段结构")
            ds.doc_form = doc_form
        if count + len(files) > self._rt.settings.max_documents_per_dataset:
            raise Invalid(f"单个知识库最多 {self._rt.settings.max_documents_per_dataset} 份文档")
        exts = [self._check_file(f) for f in files]
        mode, rules = self._rule(doc_form, process_rule)

        rule = ProcessRule(dataset_uuid=ds.uuid, mode=mode, rules=rules, created_by=access.p.uid)
        self._s.add(rule)
        await self._s.flush()
        ds.latest_process_rule_uuid = rule.uuid

        max_pos = int(
            await self._s.scalar(
                select(func.coalesce(func.max(Document.position), 0)).where(
                    Document.dataset_uuid == ds.uuid
                )
            )
            or 0
        )
        batch = uuid4().hex
        docs: list[Document] = []
        for i, (f, ext) in enumerate(zip(files, exts, strict=True), start=1):
            doc_uuid = uuid4()
            base = f.filename.rsplit("/", 1)[-1]
            key = f"{self._rt.settings.oss_prefix}/{ds.uuid}/{doc_uuid}/{base}"
            mime = f.content_type or mimetypes.guess_type(f.filename)[0]
            await asyncio.to_thread(self._rt.storage.put, key, f.data, mime)
            doc = Document(
                uuid=doc_uuid,
                dataset_uuid=ds.uuid,
                process_rule_uuid=rule.uuid,
                batch=batch,
                position=max_pos + i,
                data_source_type="upload_file",
                data_source_info={
                    "object_key": key,
                    "filename": f.filename,
                    "size": len(f.data),
                    "mime": mime,
                    "extension": ext,
                },
                name=f.filename[:255],
                doc_form=doc_form,
                indexing_status="waiting",
                review_status="pending" if ds.review_required else "none",
                doc_metadata=doc_metadata,
                acl=acl,
                valid_from=valid_from,
                valid_to=valid_to,
                created_by=access.p.uid,
            )
            if supersedes:
                await meta_svc.link_version(access, doc, UUID(str(supersedes)))
            self._s.add(doc)
            docs.append(doc)
        await self._s.commit()
        self._rt.dispatcher.indexing([str(d.uuid) for d in docs])
        return batch, docs

    # ------------------------------------------------------------------ 状态

    async def pause(self, access: DatasetAccess, doc: Document) -> Document:
        access.require_write()
        if doc.indexing_status not in _RUNNING:
            raise Conflict("只有入库中的文档可以暂停")
        await self._rt.redis.set(pause_key(doc.uuid), "1")
        doc.paused_by = access.p.uid
        doc.paused_at = utcnow()
        if doc.indexing_status == "waiting":
            doc.indexing_status = "paused"
        return doc

    async def resume(self, access: DatasetAccess, doc: Document) -> Document:
        access.require_write()
        if doc.indexing_status not in ("paused", *_RUNNING):
            raise Conflict("只有已暂停的文档可以恢复")
        await self._rt.redis.delete(pause_key(doc.uuid))
        doc.indexing_status = "waiting"
        doc.paused_at = None
        doc.paused_by = None
        await self._s.commit()
        self._rt.dispatcher.indexing([str(doc.uuid)])
        return doc

    async def retry(self, access: DatasetAccess, docs: list[Document]) -> list[Document]:
        access.require_write()
        for doc in docs:
            if doc.indexing_status != "error":
                raise Conflict(f"文档「{doc.name}」不是失败状态，不需要重试")
            await self._rt.redis.delete(pause_key(doc.uuid))
            doc.indexing_status = "waiting"
            doc.error = None
        await self._s.commit()
        self._rt.dispatcher.indexing([str(d.uuid) for d in docs])
        return docs

    async def _sync_doc_ok(self, access: DatasetAccess, docs: list[Document]) -> None:
        for doc in docs:
            await sync_doc_payload(self._s, self._rt, access.ds, doc)

    async def set_status(self, access: DatasetAccess, docs: list[Document], action: str) -> None:
        access.require_write()
        for doc in docs:
            if action == "enable":
                doc.enabled = True
            elif action == "disable":
                doc.enabled = False
            elif action == "archive":
                doc.archived = True
            elif action == "un_archive":
                doc.archived = False
            else:
                raise Invalid(f"未知操作：{action}")
        await self._s.flush()
        await self._sync_doc_ok(access, docs)

    async def review(
        self, access: DatasetAccess, docs: list[Document], *, approve: bool, note: str | None
    ) -> None:
        access.require_review()
        for doc in docs:
            if doc.review_status not in ("pending", "rejected", "approved"):
                raise Conflict(f"文档「{doc.name}」不需要审核")
            doc.review_status = "approved" if approve else "rejected"
            doc.reviewed_by = access.p.uid
            doc.reviewed_at = utcnow()
            doc.review_note = note
        await self._s.flush()
        await self._sync_doc_ok(access, docs)

    async def rename(self, access: DatasetAccess, doc: Document, name: str) -> Document:
        access.require_write()
        name = name.strip()
        if not name:
            raise Invalid("文档名称不能为空")
        doc.name = name[:255]
        return doc

    async def delete(self, access: DatasetAccess, docs: list[Document]) -> None:
        access.require_write()
        targets = await stores(self._s, self._rt, access.ds)
        for doc in docs:
            # 正在跑的入库任务看到暂停标记会停下；看到文档没了会直接退出
            await self._rt.redis.set(pause_key(doc.uuid), "1", ex=3600)
            for store in targets:
                await store.delete_by_document(str(doc.uuid))
            await self._s.execute(delete(ChildChunk).where(ChildChunk.document_uuid == doc.uuid))
            await self._s.execute(delete(Segment).where(Segment.document_uuid == doc.uuid))
            await self._s.delete(doc)
            prefix = f"{self._rt.settings.oss_prefix}/{access.ds.uuid}/{doc.uuid}/"
            try:
                await asyncio.to_thread(self._rt.storage.delete_prefix, prefix)
            except Exception:
                logger.warning("删除原文件失败：%s", prefix, exc_info=True)
        await self._s.flush()

    async def many(self, access: DatasetAccess, document_uuids: list[UUID]) -> list[Document]:
        if not document_uuids:
            raise Invalid("请选择文档")
        rows = (
            await self._s.scalars(
                select(Document).where(
                    Document.dataset_uuid == access.ds.uuid, Document.uuid.in_(document_uuids)
                )
            )
        ).all()
        if len(rows) != len(set(document_uuids)):
            raise NotFound("部分文档不存在")
        return list(rows)
