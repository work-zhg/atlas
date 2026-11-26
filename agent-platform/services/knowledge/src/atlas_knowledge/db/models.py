"""知识库的表（doc/atlas-rag-design.html §5，对照 Dify api/models/dataset.py）。

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。
★ 不建外键（与运行时 0013 起的约定一致）：引用完整性由 service 层保证。
★ 人员字段存用户中心用户 id（= Atlas app_user.id）或服务账号 id 的字符串形式。
★ 多租户（迁移 k0009）：除向量缓存外的表都归租户（TenantOwned），查询按当前租户自动过滤。
  向量缓存（kb_embedding_cache）是（模型, 文本哈希）→ 向量的纯函数缓存，不归任何租户：
  只有拿着同一段原文才查得到，查到的也只是这段原文自己的向量。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from atlas_tenancy import TenantOwned
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
    text,
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
#: 文档的分段结构（同 Dify doc_form）
DOC_FORMS = ("text_model", "hierarchical_model", "qa_model")
#: 知识库的可见性（知识集方案）：public = 大家都能用（任何人至少可读）；private = 按用户中心的
#: 数据权限。写、管理（上传、改设置、共享、删除）两种都按数据权限
VISIBILITIES = ("public", "private")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Row:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid4, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class KnowledgeSet(TenantOwned, _Row, Base):
    """知识集：把同一主题的知识库编成一组（doc/design/knowledge-set-design.html）。

    智能体按知识集使用知识：名称和介绍进智能体提示词里的「可查的知识」，由维护人写。
    ★ 只是分组，不改变权限：谁能用一个库仍按库的可见性和数据权限；知识集对一个人可见 =
      其中至少有一个库他能读，或者他是维护人。
    ★ 一个知识库只属于一个知识集（kb_dataset.set_uuid）。
    """

    __tablename__ = "kb_knowledge_set"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 给智能体看的介绍：里面有什么、能回答哪类问题
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(64), default="📁", nullable=False)
    #: 维护人（用户中心 uid）：能改名称、介绍、检索设置，能在里面新建知识库。建的人默认在内
    maintainers: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    #: 智能体在这个知识集里查一次，最多带回几段
    top_k: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class Dataset(TenantOwned, _Row, Base):
    __tablename__ = "kb_dataset"

    #: 所属知识集（kb_knowledge_set.uuid）。升级时按原来的库 1:1 建的知识集与库同一个 uuid
    set_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: public | private（VISIBILITIES）
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(64), default="📚", nullable=False)
    #: 建库时按谁判的人（auth-uid）；库级权限以用户中心为准
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: ★ 不存向量模型和 collection：active 的索引版本只记在 kb_index_version
    #: 检索配置（同 Dify retrieval_model）：search_method、top_k、score_threshold_enabled、
    #: score_threshold、reranking_enable、reranking_mode、reranking_model、weights
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: 新文档默认的分段结构
    doc_form: Mapped[str] = mapped_column(String(32), default="text_model", nullable=False)
    #: 入库增强开关（doc/knowledge-enterprise-design.html §5.3 / §5.5）：上下文补全 L2、大表摘要
    enhancements: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 这个库的文件在对象存储里的目录（原文件、解析结果、图片都在下面）。多租户以后是
    #: {租户}/_knowledge/{库}；之前建的库是 _knowledge/{库}（迁移 k0009 回填，对象原地不动）
    storage_prefix: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 最近一次使用的处理规则（新文档沿用）
    latest_process_rule_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class ProcessRule(TenantOwned, _Row, Base):
    __tablename__ = "kb_process_rule"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: automatic | custom | hierarchical
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class Document(TenantOwned, _Row, Base):
    __tablename__ = "kb_document"

    dataset_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    process_rule_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    batch: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    data_source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: upload_file: {object_key, size, mime, extension}
    data_source_info: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: 原文件的字节数：算租户的知识库存储用量（ops/capacity.py）
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_form: Mapped[str] = mapped_column(String(32), nullable=False)
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
    indexing_latency: Mapped[float | None] = mapped_column(Float, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 业务元数据（§7.2）：{字段名: 值}，字段由 kb_metadata_field 定义；写进 point payload 的 meta
    doc_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 版本与时效（§7.5）：同一份文档的各版本共用 version_group；新版本入库完成后旧版本
    #: is_current = false；生效区间外的不参与检索
    version_group: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 入库巡检按状态找停在中间的文档
    __table_args__ = (Index(None, "dataset_uuid", "position"), Index(None, "indexing_status"))

    @property
    def available(self) -> bool:
        """文档级「可被检索」：启用、未归档。"""
        return self.enabled and not self.archived

    def join_version(self, old: Document) -> None:
        """成为 old 的新版本：加入它的版本组（旧文档还没有组就以它自己的 uuid 开组）。

        元数据没单独给的沿用旧版本。新版本入库完成后才取代旧版本（入库编排）。
        """
        if old.version_group is None:
            old.version_group = old.uuid
        self.version_group = old.version_group
        if self.doc_metadata is None:
            self.doc_metadata = old.doc_metadata


class Segment(TenantOwned, _Row, Base):
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
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 向量库里的 point id；父子模式下父分段不进向量库，为空
    index_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_node_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ★ 命中次数不存在这里：从检索记录汇总（KnowledgeRetrieval.hit_counts）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: waiting | indexing | completed | error
    status: Mapped[str] = mapped_column(String(16), default="waiting", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index(None, "document_uuid", "position"),)


class ChildChunk(TenantOwned, _Row, Base):
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


class QueryLog(TenantOwned, _Row, Base):
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


class Retrieval(TenantOwned, _Row, Base):
    """一次检索的过程记录：各环节的候选（retrieval/recorder.py），排查「为什么没检索到」用。

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


class IndexVersion(TenantOwned, _Row, Base):
    """知识库的向量索引版本（doc/knowledge-enterprise-design.html §6.2）。

    ★ 每个库恰好一个 active 版本（部分唯一索引），向量模型和 collection 只记在这里：切换版本 =
      在一个事务里改状态（检索按请求读 active 版本，看到的要么是旧版本、要么是新版本）。
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
    #: 构建进度：{documents, points, done: [已重写的文档], early: [版本建立前就在写的文档]}
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("dataset_uuid", "version"),
        Index(
            "ux_kb_index_version_active",
            "dataset_uuid",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


#: 元数据字段类型
METADATA_TYPES = ("string", "number", "date", "enum")


class MetadataField(TenantOwned, _Row, Base):
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


class EnhanceCacheEntry(TenantOwned, _Row, Base):
    """入库增强（上下文补全、大表摘要）的结果缓存：重跑入库、只改子块规则重新分段时不用再调模型
    （segment/enhance.py、segment/enhance_cache.py）。

    键 = 种类、模型、提示词版本、内容哈希；保留 30 天（jobs.EnhanceCacheCleanup 每天清一次）。
    ★ 放数据库而不是 Redis：读得少、对延迟不敏感，却要留 30 天、随入库量增长；平台的 Redis 是
      共用的一个实例，还装着任务队列和锁。
    ★ 带租户：删除租户时一起删；别的租户上传同一份文件也不会命中。
    """

    __tablename__ = "kb_enhance_cache"

    cache_key: Mapped[str] = mapped_column(String(512), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_uuid", "cache_key"), Index(None, "created_at"))


class TenantCapacity(TenantOwned, _Row, Base):
    """运营中心单独给这个租户定的知识库存储份额（ops/capacity.py）。没有行 = 用默认份额。"""

    __tablename__ = "kb_tenant_capacity"

    #: None = 不限
    storage_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("tenant_uuid"),)
