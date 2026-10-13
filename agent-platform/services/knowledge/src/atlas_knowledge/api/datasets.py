"""/v1 知识库、文档、分段接口（路径形状贴近 Dify 知识库 API）。"""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, Query
from fastapi import UploadFile as FastUploadFile
from pydantic import BaseModel, Field

from ..errors import Invalid
from ..rag.extractor import SUPPORTED_EXTENSIONS
from ..rag.index_processor import (
    AUTOMATIC_RULES,
    HIERARCHICAL_DEFAULT_RULES,
    STRUCTURE_TEXT_DEFAULTS,
)
from ..rag.retrieval import DEFAULT_RETRIEVAL_CONFIG
from ..services.datasets import DatasetService
from ..services.documents import DocumentService, UploadFile
from ..services.retrieval import KnowledgeRetrieval
from ..services.segments import SegmentService
from .deps import PrincipalDep, RuntimeDep, SessionDep
from .serializers import (
    child_out,
    dataset_out,
    document_out,
    query_out,
    rule_out,
    segment_out,
)

router = APIRouter(prefix="/v1")

Page = Annotated[int, Query(ge=1)]
Limit = Annotated[int, Query(ge=1, le=100)]


# ───────────────────────────────────────────── 元信息


@router.get("/meta")
async def meta(rt: RuntimeDep, p: PrincipalDep) -> dict[str, Any]:
    return {
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "upload_max_bytes": rt.settings.upload_max_bytes,
        "max_segmentation_length": rt.settings.max_segmentation_length,
        "automatic_rules": AUTOMATIC_RULES,
        "hierarchical_default_rules": HIERARCHICAL_DEFAULT_RULES,
        "structure_text_defaults": STRUCTURE_TEXT_DEFAULTS,
        "default_retrieval_config": DEFAULT_RETRIEVAL_CONFIG,
        "embedding_models": [{"provider": "fastembed", "model": rt.settings.embedding_model}],
        "rerank_models": [{"provider": "fastembed", "model": rt.settings.rerank_model}],
        "operations": sorted(p.operations & {"kb:create", "kb:manage_all", "kb:review"}),
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
    doc_form: str | None = None
    review_required: bool | None = None


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
    return {
        "data": [dataset_out(d, stats=stats.get(d.uuid)) for d in rows],
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
    return dataset_out(ds, members=await svc.members(ds.uuid), can_write=True, can_admin=True)


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
        members=await svc.members(dataset_id),
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
        ds,
        members=await svc.members(dataset_id),
        can_write=access.can_write,
        can_admin=access.is_admin,
    )


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> None:
    svc = DatasetService(session, rt)
    await svc.delete(await svc.access(dataset_id, p))


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
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    batch, docs = await DocumentService(session, rt).upload(
        access,
        await _files(files),
        process_rule=_json_form(process_rule, "process_rule"),
        doc_form=doc_form,
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


class RenameBody(BaseModel):
    name: str


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
    doc = await svc.rename(access, await svc.get(access, document_id), body.name)
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
    seg = await svc.update(
        access, doc, await svc.get(doc, segment_id), content=body.content, enabled=body.enabled
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


class HitTestingBody(BaseModel):
    query: str
    retrieval_config: dict[str, Any] | None = None


@router.post("/datasets/{dataset_id}/hit-testing")
async def hit_testing(
    dataset_id: UUID, body: HitTestingBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    return await KnowledgeRetrieval(session, rt).hit_testing(
        access, body.query, body.retrieval_config
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


class RetrieveBody(BaseModel):
    dataset_ids: list[UUID] = Field(min_length=1, max_length=20)
    query: str
    retrieval_config: dict[str, Any] | None = None


@router.post("/retrieve")
async def retrieve(
    body: RetrieveBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """多库检索（服务账号 / 前端用）：调用方须能读每个库。"""
    res = await KnowledgeRetrieval(session, rt).retrieve(
        p, body.dataset_ids, body.query, body.retrieval_config, source="api"
    )
    return {
        "query": body.query,
        "records": [r.as_dict() for r in res.records],
        "retrieval_id": res.retrieval_id,
        "trace_id": res.trace_id,
    }


@router.get("/retrievals/{retrieval_id}")
async def get_retrieval(
    retrieval_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """一次检索的过程：各环节候选的漏斗（排查「为什么没检索到 / 为什么排在后面」）。"""
    svc = KnowledgeRetrieval(session, rt)
    return await svc.funnel(await svc.get(p, retrieval_id))
