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
    IndexVersion,
    MetadataField,
    ProcessRule,
    QueryLog,
    Segment,
)
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..indexing import versions
from ..perms import Grant
from ..rag.enhance import normalize_enhancements
from ..rag.retrieval import normalize_retrieval_config
from ..rag.vdb import collection_name
from ..runtime import Runtime
from .index_versions import IndexVersionService, initial_version
from .permission import DatasetAccess, Visibility, load_dataset, visibility

__all__ = ["DatasetService"]

logger = logging.getLogger(__name__)


def _members(raw: Any) -> list[tuple[str, str]]:
    """新建时 partial_members 预设的成员：[{id, level: read|write}] 或 [id]。"""
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


_PRESET_LEVEL = {"read": "READ", "write": "WRITE"}


class DatasetService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt
        #: 最近一次 list 的可见性（接口层据此给每行标上「我的权限」，不必再问用户中心）
        self.last_visibility: Visibility | None = None

    async def access(self, dataset_uuid: UUID, p: Principal) -> DatasetAccess:
        return await load_dataset(self._s, self._rt.perms, dataset_uuid, p)

    # ------------------------------------------------------------------ 查询

    async def list(
        self, p: Principal, *, page: int = 1, limit: int = 20, keyword: str | None = None
    ) -> tuple[list[Dataset], int]:
        vis = await visibility(self._rt.perms, p)
        self.last_visibility = vis
        cond = vis.clause
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

    # ------------------------------------------------------------------ 共享（用户中心数据权限）

    async def grants(self, access: DatasetAccess) -> list[dict[str, Any]]:
        if not access.shareable:
            return []
        return await self._rt.perms.acl(str(access.ds.uuid))

    def _operator(self, access: DatasetAccess) -> str:
        access.require_admin()
        if not access.shareable:
            raise Conflict("这个知识库还没有登记到用户中心（存量服务账号的库），先执行 sync-uc")
        return access.p.auth_uid

    async def add_grants(self, access: DatasetAccess, grants: list[Grant]) -> None:
        await self._rt.perms.grant(str(access.ds.uuid), self._operator(access), grants)

    async def _own_acl(self, access: DatasetAccess, acl_id: str) -> None:
        """授权行必须属于这个库 —— 用户中心只校验它属于本应用的数据编码。"""
        if acl_id not in {a["id"] for a in await self.grants(access)}:
            raise NotFound("授权不存在")

    async def update_grant(
        self, access: DatasetAccess, acl_id: str, *, level: str | None, include_sub: bool | None
    ) -> None:
        operator = self._operator(access)
        await self._own_acl(access, acl_id)
        await self._rt.perms.update(acl_id, operator, level=level, include_sub=include_sub)

    async def revoke_grant(self, access: DatasetAccess, acl_id: str) -> None:
        operator = self._operator(access)
        await self._own_acl(access, acl_id)
        await self._rt.perms.revoke(acl_id, operator)

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
            owner_id=p.auth_uid,
            permission=permission,
            indexing_technique=technique,
            embedding_provider=provider,
            embedding_model=model_name,
            embedding_dim=model.dim,
            retrieval_config=normalize_retrieval_config(body.get("retrieval_config")),
            enhancements=normalize_enhancements(body.get("enhancements")),
            doc_form=doc_form,
            review_required=bool(body.get("review_required", False)),
            collection_name=collection_name(dataset_uuid),
            created_by=p.uid,
            owner_kind="user",
        )
        self._s.add(ds)
        await self._s.flush()
        self._s.add(initial_version(ds, p.uid))
        await self._s.flush()
        # 用户中心的初始授权：OWNER = 按谁判的那个人（auth-uid；人直接操作时就是建库人），
        # 再加预设（全员只读 / 指定成员）。
        # ★ 在本事务提交前写：用户中心失败则整个新建回滚；反过来本地提交失败只会
        #   在用户中心留下一条没有库的授权，删库清理时一并清掉
        await self._rt.perms.create(
            str(ds.uuid), ds.name, await self._preset(p.auth_uid, permission, body)
        )
        return ds

    async def _preset(self, owner: str, permission: str, body: dict[str, Any]) -> list[Grant]:
        grants = [Grant("OWNER", "user", owner)]
        if permission == "all_team":
            grants.append(Grant("READ", "dept", await self._rt.perms.root_dept(), True))
        elif permission == "partial_members":
            grants += [
                Grant(_PRESET_LEVEL[level], "user", pid)
                for pid, level in _members(body.get("members"))
                if pid != owner
            ]
        return grants

    async def update(self, access: DatasetAccess, body: dict[str, Any]) -> Dataset:
        ds = access.ds
        access.require_write()
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not name:
                raise Invalid("知识库名称不能为空")
            if name[:255] != ds.name:
                ds.name = name[:255]
                await self._rt.perms.rename(str(ds.uuid), ds.name)
        if "description" in body:
            ds.description = str(body["description"] or "")
        if "icon" in body:
            ds.icon = str(body["icon"] or "📚")[:64]
        if "retrieval_config" in body:
            ds.retrieval_config = normalize_retrieval_config(body["retrieval_config"])
        if "review_required" in body:
            ds.review_required = bool(body["review_required"])
        if "enhancements" in body:
            # 只影响之后入库（或重新入库）的文档
            ds.enhancements = normalize_enhancements(body["enhancements"])
        provider = body.get("embedding_provider") or ds.embedding_provider
        model = body.get("embedding_model") or ds.embedding_model
        if (provider, model) != (ds.embedding_provider, ds.embedding_model):
            # 换模型 = 新建索引版本，后台重建完成后自动切换（§6.2）；期间检索仍用旧模型
            access.require_admin()
            await self._s.flush()
            await IndexVersionService(self._s, self._rt).create(
                access, provider=provider, model=model, auto_activate=True
            )
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
            raise Invalid("可见范围改在「共享」里设置（/v1/datasets/{id}/grants）")
        await self._s.flush()
        return ds

    async def delete(self, access: DatasetAccess) -> None:
        access.require_admin()
        ds = access.ds
        collections = {ds.collection_name} | {
            v.collection_name for v in await versions.live_versions(self._s, ds)
        }
        for name in collections:
            await self._rt.vectors.for_collection(name).delete()
        for model in (
            ChildChunk,
            Segment,
            Document,
            ProcessRule,
            DatasetPermission,
            QueryLog,
            IndexVersion,
            MetadataField,
        ):
            await self._s.execute(delete(model).where(model.dataset_uuid == ds.uuid))  # type: ignore[attr-defined]
        await self._s.delete(ds)
        await self._s.flush()
        await self._rt.perms.cleanup(str(ds.uuid))
        prefix = f"{self._rt.settings.oss_prefix}/{ds.uuid}/"
        try:
            await asyncio.to_thread(self._rt.storage.delete_prefix, prefix)
        except Exception:  # 原文件残留不影响删除结果，留给清理任务
            logger.warning("删除知识库原文件失败：%s", prefix, exc_info=True)
