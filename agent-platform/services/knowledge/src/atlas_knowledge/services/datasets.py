"""知识库的增删改查（同 Dify services/dataset_service.py 的 DatasetService 部分）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import (
    PERMISSIONS,
    ChildChunk,
    Dataset,
    DatasetPermission,
    Document,
    ProcessRule,
    QueryLog,
    Segment,
)
from ..errors import Forbidden, Invalid
from ..rag.retrieval import normalize_retrieval_config
from ..rag.vdb import collection_name
from ..runtime import Runtime
from .permission import DatasetAccess, load_dataset, visible_clause

__all__ = ["DatasetService"]

logger = logging.getLogger(__name__)


def _members(raw: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            pid, level = item, "read"
        else:
            pid, level = str(item.get("id") or ""), str(item.get("level") or "read")
        if not pid:
            continue
        if level not in ("read", "write"):
            raise Invalid("成员权限只能是 read 或 write")
        out.append((pid, level))
    return out


class DatasetService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt

    async def access(self, dataset_uuid: UUID, p: Principal) -> DatasetAccess:
        return await load_dataset(self._s, dataset_uuid, p)

    # ------------------------------------------------------------------ 查询

    async def list(
        self, p: Principal, *, page: int = 1, limit: int = 20, keyword: str | None = None
    ) -> tuple[list[Dataset], int]:
        cond = visible_clause(p)
        q = select(Dataset).where(cond)
        cq = select(func.count()).select_from(Dataset).where(cond)
        if keyword:
            like = f"%{keyword.strip()}%"
            q = q.where(Dataset.name.ilike(like))
            cq = cq.where(Dataset.name.ilike(like))
        total = int(await self._s.scalar(cq) or 0)
        rows = await self._s.scalars(
            q.order_by(Dataset.created_at.desc()).offset((page - 1) * limit).limit(limit)
        )
        return list(rows.all()), total

    async def stats(self, dataset_uuids: list[UUID]) -> dict[UUID, dict[str, int]]:
        if not dataset_uuids:
            return {}
        rows = await self._s.execute(
            select(
                Document.dataset_uuid,
                func.count(Document.id),
                func.coalesce(func.sum(Document.word_count), 0),
            )
            .where(Document.dataset_uuid.in_(dataset_uuids))
            .group_by(Document.dataset_uuid)
        )
        return {u: {"document_count": int(c), "word_count": int(w)} for u, c, w in rows.all()}

    async def members(self, dataset_uuid: UUID) -> list[dict[str, str]]:
        rows = await self._s.scalars(
            select(DatasetPermission).where(DatasetPermission.dataset_uuid == dataset_uuid)
        )
        return [{"id": r.principal_id, "level": r.level} for r in rows.all()]

    # ------------------------------------------------------------------ 写

    async def create(self, p: Principal, body: dict[str, Any]) -> Dataset:
        if not p.has("kb:create"):
            raise Forbidden("没有新建知识库的权限")
        name = str(body.get("name") or "").strip()
        if not name:
            raise Invalid("请填写知识库名称")
        technique = body.get("indexing_technique") or "high_quality"
        if technique != "high_quality":
            raise Invalid("当前只支持高质量索引（经济模式在后续版本提供）")
        permission = body.get("permission") or "only_me"
        if permission not in PERMISSIONS:
            raise Invalid("可见范围只能是 only_me / all_team / partial_members")
        doc_form = body.get("doc_form") or "text_model"
        if doc_form not in ("text_model", "hierarchical_model"):
            raise Invalid("分段结构只能是通用（text_model）或父子（hierarchical_model）")
        provider = body.get("embedding_provider") or "fastembed"
        model_name = body.get("embedding_model") or self._rt.settings.embedding_model
        model = await asyncio.to_thread(self._rt.registry.embedding, provider, model_name)
        dataset_uuid = uuid4()
        ds = Dataset(
            uuid=dataset_uuid,
            name=name[:255],
            description=str(body.get("description") or ""),
            icon=str(body.get("icon") or "📚")[:64],
            owner_id=p.id,
            permission=permission,
            indexing_technique=technique,
            embedding_provider=provider,
            embedding_model=model_name,
            embedding_dim=model.dim,
            retrieval_config=normalize_retrieval_config(body.get("retrieval_config")),
            doc_form=doc_form,
            review_required=bool(body.get("review_required", False)),
            collection_name=collection_name(dataset_uuid),
            created_by=p.id,
        )
        self._s.add(ds)
        await self._replace_members(ds, _members(body.get("members")))
        await self._s.flush()
        return ds

    async def update(self, access: DatasetAccess, body: dict[str, Any]) -> Dataset:
        ds = access.ds
        access.require_write()
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not name:
                raise Invalid("知识库名称不能为空")
            ds.name = name[:255]
        if "description" in body:
            ds.description = str(body["description"] or "")
        if "icon" in body:
            ds.icon = str(body["icon"] or "📚")[:64]
        if "retrieval_config" in body:
            ds.retrieval_config = normalize_retrieval_config(body["retrieval_config"])
        if "review_required" in body:
            ds.review_required = bool(body["review_required"])
        if "embedding_model" in body and body["embedding_model"] != ds.embedding_model:
            raise Invalid("更换向量模型需要重建索引，当前版本暂不支持")
        if "doc_form" in body and body["doc_form"] != ds.doc_form:
            has_docs = await self._s.scalar(
                select(func.count()).select_from(Document).where(Document.dataset_uuid == ds.uuid)
            )
            if has_docs:
                raise Invalid("已有文档的知识库不能更改分段结构")
            if body["doc_form"] not in ("text_model", "hierarchical_model"):
                raise Invalid("分段结构只能是 text_model 或 hierarchical_model")
            ds.doc_form = body["doc_form"]
        if "permission" in body or "members" in body:
            access.require_admin()
            if "permission" in body:
                if body["permission"] not in PERMISSIONS:
                    raise Invalid("可见范围只能是 only_me / all_team / partial_members")
                ds.permission = body["permission"]
            if "members" in body:
                await self._replace_members(ds, _members(body["members"]))
        await self._s.flush()
        return ds

    async def _replace_members(self, ds: Dataset, members: list[tuple[str, str]]) -> None:
        await self._s.execute(
            delete(DatasetPermission).where(DatasetPermission.dataset_uuid == ds.uuid)
        )
        for pid, level in dict(members).items():
            self._s.add(DatasetPermission(dataset_uuid=ds.uuid, principal_id=pid, level=level))

    async def delete(self, access: DatasetAccess) -> None:
        access.require_admin()
        ds = access.ds
        await self._rt.vectors.for_collection(ds.collection_name).delete()
        for model in (ChildChunk, Segment, Document, ProcessRule, DatasetPermission, QueryLog):
            await self._s.execute(delete(model).where(model.dataset_uuid == ds.uuid))  # type: ignore[attr-defined]
        await self._s.delete(ds)
        await self._s.flush()
        prefix = f"{self._rt.settings.oss_prefix}/{ds.uuid}/"
        try:
            await asyncio.to_thread(self._rt.storage.delete_prefix, prefix)
        except Exception:  # 原文件残留不影响删除结果，留给清理任务
            logger.warning("删除知识库原文件失败：%s", prefix, exc_info=True)
