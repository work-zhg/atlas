"""入库编排（参考 Dify services/knowledge/indexing/execution.py 与 adapters/execution.py）。

    ① extract    读对象存储里的原文件 → 解析                           status=parsing
    ② transform  清洗 + 分段（父子模式带子块）                          status=splitting
    ③ save       分段写库（先清掉这份文档的旧分段与向量）
    ④ load       按批向量化 + 写 Qdrant                                 status=indexing
    ⑤ complete   记录字数、耗时                                         status=completed

★ 每个阶段、每一批之前检查 Redis 暂停标记 kb:doc:{uuid}:paused（同 Dify document_{id}_is_paused）。
★ 恢复：分段已经写库（splitting_completed_at 有值）就跳过 ①②③，只把未完成的分段继续 load ——
  大文件中途失败或被暂停，不必重新解析。
★ 不设到点结束的超时（Atlas 原则）：慢就慢，用暂停 / 删除控制。
★ 每个阶段一个短事务：状态变化要让 API 立刻看得到。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import ChildChunk, Dataset, Document, ProcessRule, Segment
from ..db.types import utcnow
from ..rag import index_processor
from ..rag.extractor import extract
from ..rag.models import Document as RagDocument
from ..runtime import Runtime
from ..telemetry import tracer
from .loader import index_segments

__all__ = ["DocumentPaused", "IndexingRunner", "is_paused", "pause_key"]

logger = logging.getLogger(__name__)

_LOAD_BATCH = 64


def pause_key(document_uuid: str | UUID) -> str:
    return f"kb:doc:{document_uuid}:paused"


def lock_key(document_uuid: str | UUID) -> str:
    return f"kb:doc:{document_uuid}:indexing"


async def is_paused(redis: Any, document_uuid: str | UUID) -> bool:
    return bool(redis is not None and await redis.exists(pause_key(document_uuid)))


class DocumentPaused(Exception):
    pass


class IndexingRunner:
    def __init__(self, rt: Runtime, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._rt = rt
        self._sm = sessionmaker

    async def _check_pause(self, doc_uuid: UUID) -> None:
        if await is_paused(self._rt.redis, doc_uuid):
            raise DocumentPaused(str(doc_uuid))

    async def _set(self, doc_uuid: UUID, **values: Any) -> None:
        async with self._sm() as s:
            doc = await s.scalar(select(Document).where(Document.uuid == doc_uuid))
            if doc is None:
                return
            for k, v in values.items():
                setattr(doc, k, v)
            await s.commit()

    async def run(self, document_uuid: str | UUID) -> None:
        doc_uuid = UUID(str(document_uuid))
        redis = self._rt.redis
        # ★ 同一文档同时只有一个入库任务；锁的过期只是兜底（进程被杀时自动释放），不是超时
        if redis is not None and not await redis.set(lock_key(doc_uuid), "1", nx=True, ex=6 * 3600):
            logger.info("文档 %s 已有入库任务在跑，跳过", doc_uuid)
            return
        try:
            # ★ 入库是独立链路（任务经 Celery 异步投递，不挂在上传请求下）；按文档 id 查
            with tracer().start_as_current_span("knowledge.ingest") as span:
                span.set_attribute("knowledge.document_uuid", str(doc_uuid))
                await self._run(doc_uuid)
        except DocumentPaused:
            logger.info("文档 %s 已暂停", doc_uuid)
            await self._set(doc_uuid, indexing_status="paused", paused_at=utcnow())
        except Exception as exc:
            logger.exception("文档 %s 入库失败", doc_uuid)
            await self._set(doc_uuid, indexing_status="error", error=str(exc)[:2000])
        finally:
            if redis is not None:
                await redis.delete(lock_key(doc_uuid))

    async def _run(self, doc_uuid: UUID) -> None:
        async with self._sm() as s:
            doc = await s.scalar(select(Document).where(Document.uuid == doc_uuid))
            if doc is None:
                logger.info("文档 %s 已删除，跳过入库", doc_uuid)
                return
            if doc.indexing_status == "completed":
                return
            ds = await s.scalar(select(Dataset).where(Dataset.uuid == doc.dataset_uuid))
            rule = await s.scalar(
                select(ProcessRule).where(ProcessRule.uuid == doc.process_rule_uuid)
            )
            if ds is None or rule is None:
                raise RuntimeError("知识库或处理规则不存在")
            resume = doc.splitting_completed_at is not None
            started = time.monotonic()
            if doc.processing_started_at is None:
                doc.processing_started_at = utcnow()
            doc.error = None
            doc.paused_at = None
            await s.commit()

        if not resume:
            await self._check_pause(doc_uuid)
            await self._set(doc_uuid, indexing_status="parsing")
            with tracer().start_as_current_span("knowledge.ingest.parse") as span:
                parsed = await self._extract(doc)
                span.set_attribute("knowledge.pages", len(parsed))
                span.set_attribute("knowledge.chars", sum(len(d.page_content) for d in parsed))
            await self._set(doc_uuid, indexing_status="splitting", parsing_completed_at=utcnow())

            await self._check_pause(doc_uuid)
            with tracer().start_as_current_span("knowledge.ingest.split") as span:
                span.set_attribute("knowledge.doc_form", doc.doc_form)
                nodes = await asyncio.to_thread(
                    index_processor.transform, parsed, doc_form=doc.doc_form, rules=rule.rules
                )
                span.set_attribute("knowledge.segments", len(nodes))
                span.set_attribute(
                    "knowledge.child_chunks", sum(len(n.children or []) for n in nodes)
                )
            if not nodes:
                raise RuntimeError("没有解析出任何文本内容")
            with tracer().start_as_current_span("knowledge.ingest.save"):
                await self._save(doc, nodes)

        await self._set(doc_uuid, indexing_status="indexing")
        with tracer().start_as_current_span("knowledge.ingest.load") as span:
            span.set_attribute("knowledge.resume", resume)
            await self._load(ds, doc_uuid)

        async with self._sm() as s:
            word_count = await s.scalar(
                select(func.coalesce(func.sum(Segment.word_count), 0)).where(
                    Segment.document_uuid == doc_uuid
                )
            )
            doc2 = await s.scalar(select(Document).where(Document.uuid == doc_uuid))
            if doc2 is None:
                return
            doc2.indexing_status = "completed"
            doc2.completed_at = utcnow()
            doc2.word_count = int(word_count or 0)
            doc2.indexing_latency = round(time.monotonic() - started, 3)
            await s.commit()

    async def _extract(self, doc: Document) -> list[RagDocument]:
        info = doc.data_source_info
        if doc.data_source_type != "upload_file":
            raise RuntimeError(f"暂不支持的数据源：{doc.data_source_type}")
        data = await asyncio.to_thread(self._rt.storage.get, info["object_key"])
        return await asyncio.to_thread(extract, info.get("filename") or doc.name, data)

    async def _save(self, doc: Document, nodes: list[RagDocument]) -> None:
        """清掉旧分段与旧向量，写新分段。一个事务。"""
        async with self._sm() as s:
            ds = await s.scalar(select(Dataset).where(Dataset.uuid == doc.dataset_uuid))
            assert ds is not None
            await self._rt.vectors.for_collection(ds.collection_name).delete_by_document(
                str(doc.uuid)
            )
            await s.execute(delete(ChildChunk).where(ChildChunk.document_uuid == doc.uuid))
            await s.execute(delete(Segment).where(Segment.document_uuid == doc.uuid))
            for pos, node in enumerate(nodes, start=1):
                seg = Segment(
                    uuid=UUID(node.metadata["doc_id"]),
                    dataset_uuid=doc.dataset_uuid,
                    document_uuid=doc.uuid,
                    position=pos,
                    content=node.page_content,
                    title_path=node.metadata.get("title_path") or None,
                    word_count=len(node.page_content),
                    status="waiting",
                    created_by=doc.created_by,
                )
                s.add(seg)
                for cpos, child in enumerate(node.children or [], start=1):
                    s.add(
                        ChildChunk(
                            uuid=UUID(child.metadata["doc_id"]),
                            dataset_uuid=doc.dataset_uuid,
                            document_uuid=doc.uuid,
                            segment_uuid=seg.uuid,
                            position=cpos,
                            content=child.page_content,
                            context=child.metadata.get("context") or None,
                            word_count=len(child.page_content),
                            created_by=doc.created_by,
                        )
                    )
            d = await s.scalar(select(Document).where(Document.uuid == doc.uuid))
            if d is not None:
                d.splitting_completed_at = utcnow()
            await s.commit()

    async def _load(self, ds: Dataset, doc_uuid: UUID) -> None:
        while True:
            await self._check_pause(doc_uuid)
            async with self._sm() as s:
                doc = await s.scalar(select(Document).where(Document.uuid == doc_uuid))
                if doc is None:
                    return
                batch = (
                    await s.scalars(
                        select(Segment)
                        .where(Segment.document_uuid == doc_uuid, Segment.status != "completed")
                        .order_by(Segment.position)
                        .limit(_LOAD_BATCH)
                    )
                ).all()
                if not batch:
                    return
                with tracer().start_as_current_span("knowledge.ingest.load_batch") as span:
                    span.set_attribute("knowledge.segments", len(batch))
                    await index_segments(s, self._rt, ds, doc, batch)
                await s.commit()
