"""知识库的表（doc/atlas-rag-design.html §5，对照 Dify api/models/dataset.py）。

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。
★ 不建外键（与运行时 0013 起的约定一致）：引用完整性由 service 层保证。
★ 人员字段存用户中心用户 id（= Atlas app_user.id）或服务账号 id 的字符串形式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

#: 文档入库状态（同 Dify documents.indexing_status）
INDEXING_STATUSES = (
    "waiting",
    "parsing",
    "splitting",
    "indexing",
    "completed",
    "error",
    "paused",
)
#: 审核状态：none = 库不要求审核；pending / approved / rejected
REVIEW_STATUSES = ("none", "pending", "approved", "rejected")
#: 可见范围（同 Dify DatasetPermissionEnum）
PERMISSIONS = ("only_me", "all_team", "partial_members")
#: 文档的分段结构（同 Dify doc_form）
DOC_FORMS = ("text_model", "hierarchical_model", "qa_model")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Row:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid4, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Dataset(_Row, Base):
    __tablename__ = "kb_dataset"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(64), default="📚", nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    permission: Mapped[str] = mapped_column(String(32), default="only_me", nullable=False)
    #: high_quality = 向量；economy = 关键词（P2）
    indexing_technique: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(64), default="fastembed", nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 检索配置（同 Dify retrieval_model）：search_method、top_k、score_threshold_enabled、
    #: score_threshold、reranking_enable、reranking_mode、reranking_model、weights
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: 新文档默认的分段结构
    doc_form: Mapped[str] = mapped_column(String(32), default="text_model", nullable=False)
    #: Atlas 扩展：入库审核（doc §11.3）
    review_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 最近一次使用的处理规则（新文档沿用）
    latest_process_rule_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: internal | external（P2）
    provider: Mapped[str] = mapped_column(String(16), default="internal", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class DatasetPermission(_Row, Base):
    """partial_members 的成员表（同 Dify dataset_permissions）。level: read | write。"""

    __tablename__ = "kb_dataset_permission"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="read", nullable=False)

    __table_args__ = (UniqueConstraint("dataset_uuid", "principal_id"),)


class ProcessRule(_Row, Base):
    __tablename__ = "kb_process_rule"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: automatic | custom | hierarchical
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class Document(_Row, Base):
    __tablename__ = "kb_document"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    process_rule_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    batch: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    data_source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: upload_file: {object_key, size, mime, extension}
    data_source_info: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_form: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    indexing_status: Mapped[str] = mapped_column(String(16), default="waiting", nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    parsing_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    splitting_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    paused_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    indexing_latency: Mapped[float | None] = mapped_column(Float, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index(None, "dataset_uuid", "position"),)

    @property
    def available(self) -> bool:
        """文档级「可被检索」：启用、未归档、审核不卡（none / approved）。"""
        return self.enabled and not self.archived and self.review_status in ("none", "approved")


class Segment(_Row, Base):
    __tablename__ = "kb_segment"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    document_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 按标题分段时的标题路径（如 ["项目经验", "五、资信平台"]）；建向量与返回给模型时拼在前面，
    #: 不进正文 —— 正文保持原文
    title_path: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    #: 问答模式的答案（P2）
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    keywords: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    #: 向量库里的 point id；父子模式下父分段不进向量库，为空
    index_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_node_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: waiting | indexing | completed | error
    status: Mapped[str] = mapped_column(String(16), default="waiting", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index(None, "document_uuid", "position"),)


class ChildChunk(_Row, Base):
    __tablename__ = "kb_child_chunk"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    document_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    segment_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 建向量时拼在正文前的上下文（标题路径，表格行再加表头）；正文仍是原文
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    index_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_node_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: automatic | customized
    type: Mapped[str] = mapped_column(String(16), default="automatic", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index(None, "segment_uuid", "position"),)


class EmbeddingCache(_Row, Base):
    """（模型, 文本哈希）→ 向量（同 Dify embeddings 表）。"""

    __tablename__ = "kb_embedding_cache"

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: float32 小端字节串
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (UniqueConstraint("provider", "model", "text_hash"),)


class QueryLog(_Row, Base):
    """检索记录（同 Dify dataset_queries）。"""

    __tablename__ = "kb_query_log"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    #: hit_testing | agent | api | mcp
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 对应的检索过程记录（kb_retrieval.uuid）；老记录为空
    retrieval_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)


class Retrieval(_Row, Base):
    """一次检索的过程记录：各环节的候选（rag/recorder.py），排查「为什么没检索到」用。

    ★ 一次检索一条（可能跨多个库），kb_query_log 按库各一条并指回这里。
    ★ 候选只存 id / 分数 / 名次，正文在看的时候从分段表取。
    """

    __tablename__ = "kb_retrieval"

    dataset_uuids: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 实际生效的检索设置（多库时是收口设置，各库自己的设置在 stages 的 attrs 里不重复存）
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: [{name, dataset_uuid, elapsed_ms, attrs, items: [{point, segment, kind, score}]}]
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    #: 最终结果 [{segment, score}]
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    #: 本次检索所在链路（遥测关闭时为空）
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
