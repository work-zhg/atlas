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
#: 新建时的共享预设（同 Dify DatasetPermissionEnum 的取值）。★ 只在新建时用来写入用户中心的
#: 初始授权；之后的共享以用户中心数据权限为准（perms.py），本服务不再保存可见范围
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
    #: user = 已登记到用户中心，库级权限全在用户中心。service = 存量：服务账号建的、没有登记，
    #: 只有「全部知识库」授权的人能用。去服务账号后新库都是 user；`sync-uc --service-owner`
    #: 把存量改归一个人（内网身份传递 §14），之后这一列可以删掉
    owner_kind: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    #: ★ 已废弃：接入用户中心前的可见范围。只有一次性迁移命令（sync-uc）还会读它
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
    #: 入库增强开关（doc/knowledge-enterprise-design.html §5.3 / §5.5）：上下文补全 L2、大表摘要
    enhancements: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 最近一次使用的处理规则（新文档沿用）
    latest_process_rule_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: internal | external（P2）
    provider: Mapped[str] = mapped_column(String(16), default="internal", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class DatasetPermission(_Row, Base):
    """★ 已废弃：接入用户中心前 partial_members 的成员表。只有迁移命令（sync-uc）还会读它。"""

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
    #: 解析质量报告（doc/knowledge-enterprise-design.html §4.7）：页数、扫描页、乱码比例、告警……
    parse_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: DocIR 在对象存储里的 key：解析预览、换分段规则时不必重新解析
    docir_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

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
    #: 业务元数据（§7.2）：{字段名: 值}，字段由 kb_metadata_field 定义；写进 point payload 的 meta
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 文档级权限（§7.4）：可访问主体标签，如 ["u:123", "d:45", "r:hr"]；None = 库内公开
    acl: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    #: 版本与时效（§7.5）：同一份文档的各版本共用 version_group；新版本入库完成后旧版本
    #: is_current = false；生效区间外的不参与检索
    version_group: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
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
    #: 引用定位（§4.6）：{"pages": [...], "boxes": [{"page", "bbox"}], "assets": [图片 key]}
    source_locator: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 向量化前缀（文档标题 + 标题路径 + LLM 上下文，§5.3）；正文仍是原文。老分段为空，用标题路径
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 分段级元数据覆盖（§7.2）：与文档元数据合并（同名以分段为准）后写进 payload 的 meta
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
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


#: 索引版本状态：building 构建中 → ready 可切换 → active 当前 → retired 已退役（collection 保留，
#: 可回滚）→ deleted（collection 已删）；failed 构建失败
INDEX_VERSION_STATUSES = ("building", "ready", "active", "retired", "deleted", "failed")
#: collection 还在的版本：写入（双写）、删除、开关都要同步到这些版本
LIVE_INDEX_STATUSES = ("building", "ready", "active", "retired")


class IndexVersion(_Row, Base):
    """知识库的向量索引版本（doc/knowledge-enterprise-design.html §6.2）。

    kb_dataset 上的 embedding_* 与 collection_name 是 active 版本的指针：切换版本 = 在一个事务里
    改这几个字段（检索按请求读知识库，看到的要么是旧版本、要么是新版本）。
    """

    __tablename__ = "kb_index_version"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 构建完成后自动切换
    auto_activate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 构建进度：{documents, done, points}
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("dataset_uuid", "version"),)


#: 元数据字段类型
METADATA_TYPES = ("string", "number", "date", "enum")


class MetadataField(_Row, Base):
    """知识库的元数据字段定义（§7.2）。值存在 kb_document.doc_metadata，检索时转成 payload 过滤。"""

    __tablename__ = "kb_metadata_field"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: string | number | date | enum
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: enum 的可选值
    options: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    multi: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 允许查询理解从问题里抽取这个字段的过滤条件
    llm_extract: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (UniqueConstraint("dataset_uuid", "name"),)
