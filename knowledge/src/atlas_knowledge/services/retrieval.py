"""召回测试与多库检索的入口：权限 + 检索 + 记录（同 Dify HitTestingService / DatasetRetrieval）。"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import Dataset, QueryLog
from ..errors import Invalid, NotFound
from ..rag.retrieval import RetrievalService, RetrievedSegment
from ..runtime import Runtime
from .permission import DatasetAccess, load_dataset

__all__ = ["KnowledgeRetrieval"]

_MAX_QUERY = 2000


def _query(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise Invalid("请输入查询内容")
    if len(text) > _MAX_QUERY:
        raise Invalid(f"查询内容不能超过 {_MAX_QUERY} 字")
    return text


class KnowledgeRetrieval:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._svc = RetrievalService(
            session,
            registry=rt.registry,
            vectors=rt.vectors,
            redis=rt.redis,
            query_ttl_s=rt.settings.query_embedding_ttl_s,
        )

    def _log(
        self,
        datasets: list[Dataset],
        query: str,
        source: str,
        ref: str | None,
        actor: str,
        hits: list[RetrievedSegment],
    ) -> None:
        for ds in datasets:
            n = sum(1 for h in hits if h.dataset_uuid == str(ds.uuid))
            self._s.add(
                QueryLog(
                    dataset_uuid=ds.uuid,
                    query=query[:_MAX_QUERY],
                    source=source,
                    source_ref=ref,
                    hits=n,
                    created_by=actor,
                )
            )

    async def hit_testing(
        self, access: DatasetAccess, query: str, retrieval_config: dict[str, Any] | None
    ) -> dict[str, Any]:
        query = _query(query)
        started = time.monotonic()
        records = await self._svc.retrieve(access.ds, query, retrieval_config)
        self._log([access.ds], query, "hit_testing", None, access.p.id, records)
        return {
            "query": query,
            "records": [r.as_dict() for r in records],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    async def retrieve(
        self,
        p: Principal | None,
        dataset_uuids: list[UUID],
        query: str,
        retrieval_config: dict[str, Any] | None,
        *,
        source: str,
        source_ref: str | None = None,
        actor: str | None = None,
    ) -> list[RetrievedSegment]:
        """p 为 None = 内部调用（atlas-server 已按智能体绑定判过权限）。"""
        query = _query(query)
        if not dataset_uuids:
            raise Invalid("请选择知识库")
        datasets: list[Dataset] = []
        if p is not None:
            for u in dict.fromkeys(dataset_uuids):
                datasets.append((await load_dataset(self._s, u, p)).ds)
        else:
            rows = (
                await self._s.scalars(
                    select(Dataset).where(Dataset.uuid.in_(list(set(dataset_uuids))))
                )
            ).all()
            if not rows:
                raise NotFound("知识库不存在")
            datasets = list(rows)
        records = await self._svc.multiple_retrieve(datasets, query, retrieval_config or {})
        self._log(datasets, query, source, source_ref, actor or (p.id if p else "system"), records)
        return records

    async def queries(self, access: DatasetAccess, *, page: int, limit: int) -> list[QueryLog]:
        rows = await self._s.scalars(
            select(QueryLog)
            .where(QueryLog.dataset_uuid == access.ds.uuid)
            .order_by(QueryLog.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        return list(rows.all())
