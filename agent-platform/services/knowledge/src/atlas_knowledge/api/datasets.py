"""/v1 知识库、文档、分段接口（路径形状贴近 Dify 知识库 API）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Response
from fastapi import UploadFile as FastUploadFile
from pydantic import BaseModel, Field

from ..errors import Invalid, NotFound
from ..perms import Grant
from ..rag.filters import BUILTIN_FIELDS
from ..rag.index_processor import (
    AUTOMATIC_RULES,
    HIERARCHICAL_DEFAULT_RULES,
    STRUCTURE_TEXT_DEFAULTS,
)
from ..rag.model_platform import parse_client
from ..rag.parsers import SUPPORTED_EXTENSIONS
from ..rag.retrieval import DEFAULT_RETRIEVAL_CONFIG
from ..services.datasets import DatasetService
from ..services.documents import DocumentService, UploadFile
from ..services.index_versions import IndexVersionService
from ..services.metadata import MetadataService
from ..services.retrieval import KnowledgeRetrieval, RetrieveOptions
from ..services.segments import SegmentService
from .deps import PrincipalDep, RuntimeDep, SessionDep
from .serializers import (
    child_out,
    dataset_out,
    document_out,
    index_version_out,
    metadata_field_out,
    query_out,
    rule_out,
    segment_out,
)

router = APIRouter(prefix="/v1")
logger = logging.getLogger(__name__)

Page = Annotated[int, Query(ge=1)]
Limit = Annotated[int, Query(ge=1, le=100)]


# ───────────────────────────────────────────── 元信息


async def _key_models(rt: Any, spec: str | None) -> list[str]:
    """服务级客户端 Key 此刻可用的模型；没配、不可用、平台暂时不可用都给空（只列本地模型）。"""
    client = parse_client(spec)
    if client is None:
        return []
    try:
        found = await asyncio.to_thread(rt.service_keys.get, client)
    except Exception:
        logger.warning("模型管理平台不可用，建库时只列本地模型", exc_info=True)
        return []
    return list(found.models) if found.usable else []


async def _remote_options(rt: Any) -> tuple[list[str], list[str]]:
    """可选的远程嵌入、重排模型。经模型管理平台时是 embedding_client / rerank_client
    这两把客户端 Key 的可用模型（agent-platform/doc/design/model-design.html 第 9 章）。
    没接时照旧按部署配置。"""
    if rt.service_keys is None:
        return (
            list(rt.settings.embedding_remote_models) if rt.settings.embedding_base_url else [],
            list(rt.settings.rerank_remote_models) if rt.settings.rerank_base_url else [],
        )
    return (
        await _key_models(rt, rt.settings.embedding_client),
        await _key_models(rt, rt.settings.rerank_client),
    )


@router.get("/meta")
async def meta(rt: RuntimeDep, p: PrincipalDep) -> dict[str, Any]:
    embeddings, rerankers = await _remote_options(rt)
    # LiteLLM 的 /v1/rerank 是 Cohere 形状
    rerank_provider = (
        "cohere_compatible" if rt.service_keys is not None else rt.settings.rerank_remote_provider
    )
    return {
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "upload_max_bytes": rt.settings.upload_max_bytes,
        "max_segmentation_length": rt.settings.max_segmentation_length,
        "automatic_rules": AUTOMATIC_RULES,
        "hierarchical_default_rules": HIERARCHICAL_DEFAULT_RULES,
        "structure_text_defaults": STRUCTURE_TEXT_DEFAULTS,
        "default_retrieval_config": DEFAULT_RETRIEVAL_CONFIG,
        "embedding_models": [
            {"provider": "fastembed", "model": rt.settings.embedding_model},
            *({"provider": "openai_compatible", "model": m} for m in embeddings),
        ],
        "rerank_models": [
            {"provider": "fastembed", "model": rt.settings.rerank_model},
            *({"provider": rerank_provider, "model": m} for m in rerankers),
        ],
        "operations": sorted(p.operations & {"kb:create", "kb:review"}),
        "trace_url_template": rt.settings.trace_url_template,
    }


# ───────────────────────────────────────────── 知识库


class DatasetBody(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    permission: str | None = None
    members: list[Any] | None = None
    indexing_technique: str | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    retrieval_config: dict[str, Any] | None = None
    enhancements: dict[str, Any] | None = None
    doc_form: str | None = None
    review_required: bool | None = None


class GrantSubject(BaseModel):
    type: Literal["user", "dept"]
    id: str


class GrantBody(BaseModel):
    """共享（用户中心数据权限）：READ 只读 / WRITE 读写 / OWNER 所有者。"""

    level: Literal["READ", "WRITE", "OWNER"]
    subjects: list[GrantSubject] = Field(min_length=1)
    include_sub: bool = True


class GrantPatch(BaseModel):
    level: Literal["READ", "WRITE", "OWNER"] | None = None
    include_sub: bool | None = None


@router.get("/datasets")
async def list_datasets(
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    page: Page = 1,
    limit: Limit = 20,
    keyword: str | None = None,
) -> dict[str, Any]:
    svc = DatasetService(session, rt)
    rows, total = await svc.list(p, page=page, limit=limit, keyword=keyword)
    stats = await svc.stats([d.uuid for d in rows])
    vis = svc.last_visibility
    return {
        "data": [
            dataset_out(d, stats=stats.get(d.uuid), level=vis.level(d) if vis else None)
            for d in rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.post("/datasets", status_code=201)
async def create_dataset(
    body: DatasetBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    svc = DatasetService(session, rt)
    ds = await svc.create(p, body.model_dump(exclude_unset=True))
    return dataset_out(ds, level="OWNER", can_write=True, can_admin=True)


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    svc = DatasetService(session, rt)
    access = await svc.access(dataset_id, p)
    stats = await svc.stats([dataset_id])
    return dataset_out(
        access.ds,
        stats=stats.get(dataset_id),
        level=access.level,
        can_write=access.can_write,
        can_admin=access.is_admin,
    )


@router.patch("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: UUID, body: DatasetBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    svc = DatasetService(session, rt)
    access = await svc.access(dataset_id, p)
    ds = await svc.update(access, body.model_dump(exclude_unset=True))
    return dataset_out(
        ds, level=access.level, can_write=access.can_write, can_admin=access.is_admin
    )


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> None:
    svc = DatasetService(session, rt)
    await svc.delete(await svc.access(dataset_id, p))


# ───────────────────────────────────────────── 共享（用户中心数据权限 AtlasKnowledgeBase）


@router.get("/datasets/{dataset_id}/grants")
async def list_grants(
    dataset_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """这个库的共享情况。能看到库的人都能看（与用户中心「数据授权」页同样的可见性）。"""
    svc = DatasetService(session, rt)
    access = await svc.access(dataset_id, p)
    return {
        "data": await svc.grants(access),
        "my_level": access.level,
        "can_manage": access.is_admin and access.shareable,
        "shareable": access.shareable,
    }


@router.post("/datasets/{dataset_id}/grants", status_code=201)
async def add_grants(
    dataset_id: UUID, body: GrantBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, bool]:
    svc = DatasetService(session, rt)
    access = await svc.access(dataset_id, p)
    await svc.add_grants(
        access,
        [
            Grant(body.level, s.type, s.id, body.include_sub and s.type == "dept")
            for s in body.subjects
        ],
    )
    return {"ok": True}


@router.patch("/datasets/{dataset_id}/grants/{acl_id}")
async def update_grant(
    dataset_id: UUID,
    acl_id: str,
    body: GrantPatch,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, bool]:
    svc = DatasetService(session, rt)
    access = await svc.access(dataset_id, p)
    await svc.update_grant(access, acl_id, level=body.level, include_sub=body.include_sub)
    return {"ok": True}


@router.delete("/datasets/{dataset_id}/grants/{acl_id}", status_code=204)
async def revoke_grant(
    dataset_id: UUID, acl_id: str, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> None:
    svc = DatasetService(session, rt)
    await svc.revoke_grant(await svc.access(dataset_id, p), acl_id)


# ───────────────────────────────────────────── 索引版本（换向量模型，§6.2）


class IndexVersionBody(BaseModel):
    embedding_provider: str = "fastembed"
    embedding_model: str
    #: 构建完成后自动切换；否则构建完成（ready）后手动切换（可先用 hit-testing 对比）
    auto_activate: bool = False


@router.get("/datasets/{dataset_id}/index-versions")
async def list_index_versions(
    dataset_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    rows = await IndexVersionService(session, rt).list(access.ds)
    return {"data": [index_version_out(v) for v in rows]}


@router.post("/datasets/{dataset_id}/index-versions", status_code=202)
async def create_index_version(
    dataset_id: UUID, body: IndexVersionBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    v = await IndexVersionService(session, rt).create(
        access,
        provider=body.embedding_provider,
        model=body.embedding_model,
        auto_activate=body.auto_activate,
    )
    return index_version_out(v)


@router.post("/datasets/{dataset_id}/index-versions/{version_id}/activate")
async def activate_index_version(
    dataset_id: UUID, version_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = IndexVersionService(session, rt)
    v = await svc.activate(access, await svc.get(access.ds, version_id))
    return index_version_out(v)


@router.delete("/datasets/{dataset_id}/index-versions/{version_id}", status_code=204)
async def delete_index_version(
    dataset_id: UUID, version_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> None:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = IndexVersionService(session, rt)
    await svc.delete(access, await svc.get(access.ds, version_id))


# ───────────────────────────────────────────── 元数据字段（§7.2）


class MetadataFieldBody(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    options: list[str] | None = None
    multi: bool = False
    llm_extract: bool = False


@router.get("/datasets/{dataset_id}/metadata-fields")
async def list_metadata_fields(
    dataset_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    rows = await MetadataService(session, rt).list_fields(access)
    builtin = [
        {
            "name": f.name,
            "type": f.type,
            "description": f.description,
            "options": [],
            "multi": False,
            "llm_extract": False,
            "builtin": True,
        }
        for f in BUILTIN_FIELDS.values()
    ]
    return {"data": [*builtin, *(metadata_field_out(r) for r in rows)]}


@router.post("/datasets/{dataset_id}/metadata-fields", status_code=201)
async def create_metadata_field(
    dataset_id: UUID, body: MetadataFieldBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    row = await MetadataService(session, rt).create_field(access, body.model_dump())
    return metadata_field_out(row)


@router.delete("/datasets/{dataset_id}/metadata-fields/{name}", status_code=204)
async def delete_metadata_field(
    dataset_id: UUID, name: str, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> None:
    access = await DatasetService(session, rt).access(dataset_id, p)
    await MetadataService(session, rt).delete_field(access, name)


# ───────────────────────────────────────────── 文档


def _json_form(raw: str | None, label: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Invalid(f"{label} 不是合法的 JSON") from exc
    if not isinstance(value, dict):
        raise Invalid(f"{label} 必须是对象")
    return value


async def _files(files: list[FastUploadFile]) -> list[UploadFile]:
    out = []
    for f in files:
        out.append(
            UploadFile(
                filename=f.filename or "unnamed", data=await f.read(), content_type=f.content_type
            )
        )
    return out


@router.post("/datasets/indexing-estimate")
async def indexing_estimate(
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    file: Annotated[FastUploadFile, File()],
    doc_form: Annotated[str, Form()] = "text_model",
    process_rule: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    (f,) = await _files([file])
    return await DocumentService(session, rt).estimate(
        f, doc_form=doc_form, process_rule=_json_form(process_rule, "process_rule")
    )


@router.post("/datasets/{dataset_id}/documents", status_code=201)
async def upload_documents(
    dataset_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    files: Annotated[list[FastUploadFile], File()],
    process_rule: Annotated[str | None, Form()] = None,
    doc_form: Annotated[str | None, Form()] = None,
    metadata: Annotated[str | None, Form()] = None,
    acl: Annotated[str | None, Form()] = None,
    valid_from: Annotated[str | None, Form()] = None,
    valid_to: Annotated[str | None, Form()] = None,
    supersedes: Annotated[UUID | None, Form()] = None,
) -> dict[str, Any]:
    """可选：metadata（JSON 对象）、acl（JSON 数组）、生效区间、
    supersedes（作为哪份文档的新版本）。"""
    access = await DatasetService(session, rt).access(dataset_id, p)
    acl_list = None
    if acl:
        try:
            acl_list = json.loads(acl)
        except ValueError as exc:
            raise Invalid("acl 不是合法的 JSON") from exc
    batch, docs = await DocumentService(session, rt).upload(
        access,
        await _files(files),
        process_rule=_json_form(process_rule, "process_rule"),
        doc_form=doc_form,
        attrs={
            "metadata": _json_form(metadata, "metadata"),
            "acl": acl_list,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "supersedes": supersedes,
        },
    )
    return {"batch": batch, "documents": [document_out(d) for d in docs]}


@router.get("/datasets/{dataset_id}/documents")
async def list_documents(
    dataset_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    page: Page = 1,
    limit: Limit = 20,
    keyword: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    rows, total = await svc.list(access, page=page, limit=limit, keyword=keyword, status=status)
    counts = await svc.segment_counts([d.uuid for d in rows])
    return {
        "data": [document_out(d, segments=counts.get(d.uuid)) for d in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/datasets/{dataset_id}/batches/{batch}/indexing-status")
async def batch_status(
    dataset_id: UUID, batch: str, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    docs = await svc.batch(access, batch)
    counts = await svc.segment_counts([d.uuid for d in docs])
    return {"data": [document_out(d, segments=counts.get(d.uuid)) for d in docs]}


@router.get("/datasets/{dataset_id}/documents/{document_id}")
async def get_document(
    dataset_id: UUID, document_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    doc = await svc.get(access, document_id)
    counts = await svc.segment_counts([doc.uuid])
    out = document_out(doc, segments=counts.get(doc.uuid))
    out["process_rule"] = rule_out(await svc.process_rule(doc.process_rule_uuid))
    return out


_ASSET_NAME = re.compile(r"^img\d+\.(png|jpg|gif|webp)$")
_ASSET_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


@router.get("/datasets/{dataset_id}/documents/{document_id}/docir")
async def get_document_docir(
    dataset_id: UUID, document_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """解析预览（doc/knowledge-enterprise-design.html §4.7）：DocIR 的块序列与解析质量报告。"""
    access = await DatasetService(session, rt).access(dataset_id, p)
    doc = await DocumentService(session, rt).get(access, document_id)
    if not doc.docir_key:
        raise NotFound("这份文档还没有解析结果（早于 DocIR 入库，或尚未解析完成）")
    raw = await asyncio.to_thread(rt.storage.get, doc.docir_key)
    return {"report": doc.parse_report or {}, "docir": json.loads(raw)}


@router.get("/datasets/{dataset_id}/documents/{document_id}/assets/{name}")
async def get_document_asset(
    dataset_id: UUID,
    document_id: UUID,
    name: str,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> Response:
    """文档里抽出的图片（分段正文里的 kb-asset:… 引用指向它）。"""
    access = await DatasetService(session, rt).access(dataset_id, p)
    doc = await DocumentService(session, rt).get(access, document_id)
    m = _ASSET_NAME.match(name)
    if m is None:
        raise NotFound("图片不存在")
    key = f"{rt.settings.oss_prefix}/{doc.dataset_uuid}/{doc.uuid}/assets/{name}"
    try:
        data = await asyncio.to_thread(rt.storage.get, key)
    except Exception as exc:
        raise NotFound("图片不存在") from exc
    return Response(
        content=data,
        media_type=_ASSET_MIME[m.group(1)],
        headers={"Cache-Control": "private, max-age=3600"},
    )


class RenameBody(BaseModel):
    """文档属性修改：名称、元数据、权限、生效区间、是否当前版本（改完只更新 payload）。"""

    name: str | None = None
    metadata: dict[str, Any] | None = None
    acl: list[str] | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    is_current: bool | None = None


@router.patch("/datasets/{dataset_id}/documents/{document_id}")
async def rename_document(
    dataset_id: UUID,
    document_id: UUID,
    body: RenameBody,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    doc = await svc.get(access, document_id)
    if body.name is not None:
        doc = await svc.rename(access, doc, body.name)
    attrs = body.model_dump(exclude_unset=True, exclude={"name"})
    if attrs:
        doc = await MetadataService(session, rt).update_document(access, doc, attrs)
    return document_out(doc)


@router.delete("/datasets/{dataset_id}/documents/{document_id}", status_code=204)
async def delete_document(
    dataset_id: UUID, document_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> None:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    await svc.delete(access, [await svc.get(access, document_id)])


async def _document_action(
    action: str,
    dataset_id: UUID,
    document_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    doc = await svc.get(access, document_id)
    if action == "pause":
        doc = await svc.pause(access, doc)
    elif action == "resume":
        doc = await svc.resume(access, doc)
    else:
        (doc,) = await svc.retry(access, [doc])
    return document_out(doc)


@router.post("/datasets/{dataset_id}/documents/{document_id}/pause")
async def pause_document(
    dataset_id: UUID, document_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    return await _document_action("pause", dataset_id, document_id, session, rt, p)


@router.post("/datasets/{dataset_id}/documents/{document_id}/resume")
async def resume_document(
    dataset_id: UUID, document_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    return await _document_action("resume", dataset_id, document_id, session, rt, p)


@router.post("/datasets/{dataset_id}/documents/{document_id}/retry")
async def retry_document(
    dataset_id: UUID, document_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    return await _document_action("retry", dataset_id, document_id, session, rt, p)


class DocumentsBody(BaseModel):
    document_ids: list[UUID] = Field(min_length=1)


@router.post("/datasets/{dataset_id}/documents-status/{action}")
async def documents_status(
    dataset_id: UUID,
    action: str,
    body: DocumentsBody,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    """批量 enable | disable | archive | un_archive | retry | delete。"""
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    docs = await svc.many(access, body.document_ids)
    if action == "retry":
        await svc.retry(access, docs)
    elif action == "delete":
        await svc.delete(access, docs)
        return {"result": "success"}
    else:
        await svc.set_status(access, docs, action)
    return {"result": "success", "documents": [document_out(d) for d in docs]}


class ReviewBody(BaseModel):
    document_ids: list[UUID] = Field(min_length=1)
    approve: bool
    note: str | None = None


@router.post("/datasets/{dataset_id}/documents-review")
async def review_documents(
    dataset_id: UUID, body: ReviewBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    svc = DocumentService(session, rt)
    docs = await svc.many(access, body.document_ids)
    await svc.review(access, docs, approve=body.approve, note=body.note)
    return {"result": "success", "documents": [document_out(d) for d in docs]}


# ───────────────────────────────────────────── 分段


async def _doc(
    session: SessionDep, rt: RuntimeDep, p: PrincipalDep, dataset_id: UUID, document_id: UUID
):  # type: ignore[no-untyped-def]
    access = await DatasetService(session, rt).access(dataset_id, p)
    doc = await DocumentService(session, rt).get(access, document_id)
    return access, doc


@router.get("/datasets/{dataset_id}/documents/{document_id}/segments")
async def list_segments(
    dataset_id: UUID,
    document_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    page: Page = 1,
    limit: Limit = 20,
    keyword: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    _, doc = await _doc(session, rt, p, dataset_id, document_id)
    segs, total, children = await SegmentService(session, rt).list(
        doc, page=page, limit=limit, keyword=keyword, enabled=enabled
    )
    hierarchical = doc.doc_form == "hierarchical_model"
    return {
        "data": [
            segment_out(s, children.get(str(s.uuid), []) if hierarchical else None) for s in segs
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "doc_form": doc.doc_form,
    }


class SegmentBody(BaseModel):
    content: str | None = None
    enabled: bool | None = None
    #: 分段级元数据覆盖（§7.2）；null = 去掉覆盖
    metadata: dict[str, Any] | None = None


@router.post("/datasets/{dataset_id}/documents/{document_id}/segments", status_code=201)
async def create_segment(
    dataset_id: UUID,
    document_id: UUID,
    body: SegmentBody,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    access, doc = await _doc(session, rt, p, dataset_id, document_id)
    svc = SegmentService(session, rt)
    seg = await svc.create(access, doc, body.content or "")
    return segment_out(
        seg, await svc.children(seg) if doc.doc_form == "hierarchical_model" else None
    )


@router.patch("/datasets/{dataset_id}/documents/{document_id}/segments/{segment_id}")
async def update_segment(
    dataset_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    body: SegmentBody,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    access, doc = await _doc(session, rt, p, dataset_id, document_id)
    svc = SegmentService(session, rt)
    extra: dict[str, Any] = {}
    if "metadata" in body.model_fields_set:
        extra["metadata"] = body.metadata
    seg = await svc.update(
        access,
        doc,
        await svc.get(doc, segment_id),
        content=body.content,
        enabled=body.enabled,
        **extra,
    )
    return segment_out(
        seg, await svc.children(seg) if doc.doc_form == "hierarchical_model" else None
    )


@router.delete(
    "/datasets/{dataset_id}/documents/{document_id}/segments/{segment_id}", status_code=204
)
async def delete_segment(
    dataset_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> None:
    access, doc = await _doc(session, rt, p, dataset_id, document_id)
    svc = SegmentService(session, rt)
    await svc.delete(access, doc, await svc.get(doc, segment_id))


class ChildBody(BaseModel):
    content: str


@router.post(
    "/datasets/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks",
    status_code=201,
)
async def add_child(
    dataset_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    body: ChildBody,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    access, doc = await _doc(session, rt, p, dataset_id, document_id)
    svc = SegmentService(session, rt)
    return child_out(await svc.add_child(access, doc, await svc.get(doc, segment_id), body.content))


@router.patch(
    "/datasets/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks/{child_id}"
)
async def update_child(
    dataset_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    child_id: UUID,
    body: ChildBody,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> dict[str, Any]:
    access, doc = await _doc(session, rt, p, dataset_id, document_id)
    svc = SegmentService(session, rt)
    child = await svc.update_child(
        access, doc, await svc.get(doc, segment_id), child_id, body.content
    )
    return child_out(child)


@router.delete(
    "/datasets/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks/{child_id}",
    status_code=204,
)
async def delete_child(
    dataset_id: UUID,
    document_id: UUID,
    segment_id: UUID,
    child_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
) -> None:
    access, doc = await _doc(session, rt, p, dataset_id, document_id)
    svc = SegmentService(session, rt)
    await svc.delete_child(access, doc, await svc.get(doc, segment_id), child_id)


# ───────────────────────────────────────────── 召回测试


class ScopeBody(BaseModel):
    """检索的企业级参数（§7）：对话历史（改写用）、元数据过滤、按时间点、含历史版本。"""

    history: list[dict[str, str]] | None = Field(default=None, max_length=50)
    filter: dict[str, Any] | None = None
    as_of: datetime | None = None
    include_superseded: bool = False

    def options(self) -> RetrieveOptions:
        return RetrieveOptions(
            history=self.history,
            filter=self.filter,
            as_of=self.as_of,
            include_superseded=self.include_superseded,
        )


class HitTestingBody(ScopeBody):
    query: str
    retrieval_config: dict[str, Any] | None = None
    #: 用指定的索引版本检索（切换前对比新旧版本，§6.2）；默认 active 版本
    index_version_id: UUID | None = None


@router.post("/datasets/{dataset_id}/hit-testing")
async def hit_testing(
    dataset_id: UUID, body: HitTestingBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    version = None
    if body.index_version_id is not None:
        version = await IndexVersionService(session, rt).get(access.ds, body.index_version_id)
    return await KnowledgeRetrieval(session, rt).hit_testing(
        access, body.query, body.retrieval_config, version=version, opts=body.options()
    )


@router.get("/datasets/{dataset_id}/queries")
async def list_queries(
    dataset_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    page: Page = 1,
    limit: Limit = 20,
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    rows = await KnowledgeRetrieval(session, rt).queries(access, page=page, limit=limit)
    return {"data": [query_out(q) for q in rows], "page": page, "limit": limit}


class RetrieveBody(ScopeBody):
    dataset_ids: list[UUID] = Field(min_length=1, max_length=20)
    query: str
    retrieval_config: dict[str, Any] | None = None


@router.post("/retrieve")
async def retrieve(
    body: RetrieveBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """多库检索（服务账号 / 前端用）：调用方须能读每个库。"""
    res = await KnowledgeRetrieval(session, rt).retrieve(
        p,
        body.dataset_ids,
        body.query,
        body.retrieval_config,
        source="api",
        opts=body.options(),
    )
    return res.as_dict(body.query)


@router.get("/retrievals/{retrieval_id}")
async def get_retrieval(
    retrieval_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """一次检索的过程：各环节候选的漏斗（排查「为什么没检索到 / 为什么排在后面」）。"""
    svc = KnowledgeRetrieval(session, rt)
    return await svc.funnel(await svc.get(p, retrieval_id))
