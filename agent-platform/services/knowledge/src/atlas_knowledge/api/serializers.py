"""库行 → 接口 JSON。字段名对齐 Dify 知识库 API，便于对照。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db.models import (
    ChildChunk,
    Dataset,
    Document,
    IndexVersion,
    MetadataField,
    ProcessRule,
    QueryLog,
    Segment,
)
from ..rag.enhance import normalize_enhancements

__all__ = ["child_out", "dataset_out", "document_out", "query_out", "rule_out", "segment_out"]


def _ts(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def dataset_out(
    ds: Dataset,
    *,
    stats: dict[str, int] | None = None,
    level: str | None = None,
    can_write: bool | None = None,
    can_admin: bool | None = None,
) -> dict[str, Any]:
    """level：当前用户的有效级别（READ / WRITE / OWNER，来自用户中心数据权限）。"""
    out: dict[str, Any] = {
        "id": str(ds.uuid),
        "name": ds.name,
        "description": ds.description,
        "icon": ds.icon,
        "owner_id": ds.owner_id,
        # service = 服务账号建的库：只有它自己能用，不能共享
        "owner_kind": ds.owner_kind,
        "indexing_technique": ds.indexing_technique,
        "embedding_provider": ds.embedding_provider,
        "embedding_model": ds.embedding_model,
        "retrieval_config": ds.retrieval_config,
        "enhancements": normalize_enhancements(ds.enhancements),
        "doc_form": ds.doc_form,
        "review_required": ds.review_required,
        "provider": ds.provider,
        "latest_process_rule_id": str(ds.latest_process_rule_uuid)
        if ds.latest_process_rule_uuid
        else None,
        "created_by": ds.created_by,
        "created_at": _ts(ds.created_at),
        "updated_at": _ts(ds.updated_at),
        "document_count": (stats or {}).get("document_count", 0),
        "word_count": (stats or {}).get("word_count", 0),
    }
    if level is not None:
        out["my_level"] = level
    if can_write is not None:
        out["can_write"] = can_write
    if can_admin is not None:
        out["can_admin"] = can_admin
    return out


def document_out(doc: Document, *, segments: tuple[int, int] | None = None) -> dict[str, Any]:
    total, done = segments or (0, 0)
    return {
        "id": str(doc.uuid),
        "dataset_id": str(doc.dataset_uuid),
        "batch": doc.batch,
        "position": doc.position,
        "name": doc.name,
        "data_source_type": doc.data_source_type,
        "data_source_info": {
            k: v
            for k, v in doc.data_source_info.items()
            if k in ("filename", "size", "mime", "extension")
        },
        "doc_form": doc.doc_form,
        "indexing_status": doc.indexing_status,
        "error": doc.error,
        "enabled": doc.enabled,
        "archived": doc.archived,
        "available": doc.available,
        "review_status": doc.review_status,
        "parse_report": doc.parse_report,
        "metadata": doc.doc_metadata or {},
        "acl": doc.acl or ["public"],
        "version_group": str(doc.version_group) if doc.version_group else None,
        "is_current": doc.is_current,
        "valid_from": _ts(doc.valid_from),
        "valid_to": _ts(doc.valid_to),
        "reviewed_by": doc.reviewed_by,
        "reviewed_at": _ts(doc.reviewed_at),
        "review_note": doc.review_note,
        "word_count": doc.word_count,
        "segment_count": total,
        "completed_segments": done,
        "indexing_latency": doc.indexing_latency,
        "processing_started_at": _ts(doc.processing_started_at),
        "parsing_completed_at": _ts(doc.parsing_completed_at),
        "splitting_completed_at": _ts(doc.splitting_completed_at),
        "completed_at": _ts(doc.completed_at),
        "paused_at": _ts(doc.paused_at),
        "created_by": doc.created_by,
        "created_at": _ts(doc.created_at),
    }


def rule_out(rule: ProcessRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {"id": str(rule.uuid), "mode": rule.mode, "rules": rule.rules}


def child_out(c: ChildChunk) -> dict[str, Any]:
    return {
        "id": str(c.uuid),
        "segment_id": str(c.segment_uuid),
        "position": c.position,
        "content": c.content,
        "context": c.context,
        "word_count": c.word_count,
        "type": c.type,
    }


def segment_out(seg: Segment, children: list[ChildChunk] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(seg.uuid),
        "document_id": str(seg.document_uuid),
        "position": seg.position,
        "content": seg.content,
        "title_path": seg.title_path or [],
        "source_locator": seg.source_locator,
        "context": seg.context,
        "metadata": seg.meta or {},
        "answer": seg.answer,
        "word_count": seg.word_count,
        "keywords": seg.keywords or [],
        "hit_count": seg.hit_count,
        "enabled": seg.enabled,
        "status": seg.status,
        "error": seg.error,
        "created_by": seg.created_by,
        "updated_by": seg.updated_by,
        "created_at": _ts(seg.created_at),
        "updated_at": _ts(seg.updated_at),
    }
    if children is not None:
        out["child_chunks"] = [child_out(c) for c in children]
    return out


def query_out(q: QueryLog) -> dict[str, Any]:
    return {
        "id": str(q.uuid),
        "query": q.query,
        "source": q.source,
        "source_ref": q.source_ref,
        "hits": q.hits,
        "retrieval_id": str(q.retrieval_uuid) if q.retrieval_uuid else None,
        "created_by": q.created_by,
        "created_at": _ts(q.created_at),
    }


def index_version_out(v: IndexVersion) -> dict[str, Any]:
    return {
        "id": str(v.uuid),
        "version": v.version,
        "embedding_provider": v.embedding_provider,
        "embedding_model": v.embedding_model,
        "embedding_dim": v.embedding_dim,
        "status": v.status,
        "auto_activate": v.auto_activate,
        "progress": v.progress,
        "error": v.error,
        "created_by": v.created_by,
        "created_at": _ts(v.created_at),
        "activated_at": _ts(v.activated_at),
    }


def metadata_field_out(f: MetadataField) -> dict[str, Any]:
    return {
        "id": str(f.uuid),
        "name": f.name,
        "type": f.type,
        "description": f.description,
        "options": f.options or [],
        "multi": f.multi,
        "llm_extract": f.llm_extract,
        "builtin": False,
    }
