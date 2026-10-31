"""MCP 网关的表（MCP 网关原型 agent-platform/doc/prototype/mcp-gateway.html）。

    mcp_service   服务：类型（MCP 代理 / REST 转换 / gRPC 转换）、可见性、连接、上游凭据、状态；
                  MCP 代理还有从上游拉取定义的结果
    mcp_tool      工具：定义（MCP 代理从上游拉来；REST / gRPC 由管理员定义）、映射、副作用、状态
    mcp_api_key   API Key：用户 Key（本人申请）与智能体 Key（应用创建）。每把 Key 在用户中心有一个
                  主体（subject_uid），授权授给它，网关判权时以它作 X-Auth-Uid
    mcp_audit     管理动作的审计（不记 Key 原文、上游凭据）

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。
★ 不建外键（与其它服务一致）：引用完整性由 service 层保证。
★ 服务没有删除：服务名是工具名前缀、用户中心里的数据标识、智能体配置里的引用，
  停用后也不能复用。
★ 上游凭据、Key 原文存库：网关调上游要用凭据原文，Atlas 要取 Key 原文。加密等安全问题后续统一设计。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow, uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

#: 服务类型：mcp = 代理已有的 MCP Server；rest / grpc = 把接口转换成 MCP 工具
SERVICE_KINDS = ("mcp", "rest", "grpc")
VISIBILITIES = ("public", "private")
SERVICE_STATUSES = ("enabled", "disabled")
#: 副作用：对应 MCP 工具注解（read = readOnlyHint，destructive = destructiveHint）
EFFECTS = ("read", "write", "destructive")
TOOL_STATUSES = ("active", "disabled")
KEY_TYPES = ("user", "client")
KEY_STATUSES = ("active", "blocked")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Row:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid7, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class McpService(_Row, Base):
    __tablename__ = "mcp_service"

    name: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    #: public = 任何有效的 Key 都能用全部工具；private = 按授给 Key 的数据权限
    visibility: Mapped[str] = mapped_column(String(8), default="private", nullable=False)
    #: 连接（不含凭据）。mcp: {url, transport}；rest: {base_url, spec_url}；
    #: grpc: {address, tls, discovery, service}
    connection: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    #: 上游凭据里不保密的部分：{type, key, where, user, token_url, scope, cert}
    credential: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    #: 上游凭据里保密的那个值（Token、密码、client_secret、私钥 …）。不回显
    credential_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: gRPC：方法发现得到的 FileDescriptorSet（序列化）。调用时据此构造请求、解析响应
    descriptor: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: 向上游传递调用者身份（X-Auth-Uid = Key 的 uid，X-Uid = 使用者）
    pass_identity: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="enabled", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # ── MCP 代理：从上游拉取工具定义
    #: 最近一次拉取的时间（成功或失败都推进，失败时 fetch_error 非空、工具定义保持旧的）
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 全部在册工具指纹的哈希：任何一个工具的任何字段变了，它就变
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 多副本只让一个去拉：领到的时间（租约，见 registry.claim_due）
    refresh_claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('mcp','rest','grpc')", name="kind"),
        CheckConstraint("visibility IN ('public','private')", name="visibility"),
        CheckConstraint("status IN ('enabled','disabled')", name="status"),
    )


class McpTool(_Row, Base):
    __tablename__ = "mcp_tool"

    service_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: 发给上游 / 模型看到的名字（模型侧是「服务名__工具名」）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: MCP 线上格式的参数定义。REST / gRPC 的由映射算出（registry 保存时写入）
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: MCP 线上格式的注解。MCP 代理取上游的；REST / gRPC 由副作用算出
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    effect: Mapped[str] = mapped_column(String(16), default="write", nullable=False)
    #: rest: {method, path, params: [...], pick, limit_kb}；
    #: grpc: {method, stream, request, response}；
    #: mcp: None
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: disabled = 管理员停用：对所有 Key 都不可用，和授权无关
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: 定义指纹（definitions.digest_of）：Atlas 据此提示定义变化
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 不能用的原因（工具名不合规、参数定义不是对象）。非空 = 不可用
    issues: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    #: 变化前的一版：{description, input_schema, annotations, digest}
    previous: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: MCP 代理：上游最近一次完整拉取里没有它 = 已删除。留着行，管理端要显示删了哪些
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 用户中心里登记过这个工具的数据对象（McpTool / 服务名/工具名）。第一次工具级授权时登记
    uc_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("service_uuid", "name"),
        CheckConstraint("effect IN ('read','write','destructive')", name="effect"),
        CheckConstraint("status IN ('active','disabled')", name="status"),
    )


class McpApiKey(_Row, Base):
    """一把 API Key。

    用户 Key：一人一把（user_uuid 唯一），本人申请，给外部客户端用。
    智能体 Key：client_type + client_code 唯一，应用调接口创建（Atlas 新建智能体时），
               管理员也能在页面上为还没有 Key 的智能体补申请。

    ★ Key 本身是权限主体：subject_uid 是它在用户中心的主体（数字员工），授权授给它。
      重置只换原文，subject_uid 和授权不变。
    ★ 原文存库：Atlas 要取原文去调网关。只有用户 Key 在申请、重置时把原文返回一次。
    """

    __tablename__ = "mcp_api_key"

    key_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 用户 Key 的持有人（用户中心 uid）
    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: 客户端类型（Agent）与编码（智能体 id）
    client_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 展示用：用户 Key 是持有人姓名，智能体 Key 是智能体名称（随改名同步）
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    subject_uid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False)
    secret: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: 最近一次生成原文的时间（申请、重置）
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("key_type IN ('user','client')", name="key_type"),
        CheckConstraint("status IN ('active','blocked')", name="status"),
        CheckConstraint(
            "(key_type = 'user') = (user_uuid IS NOT NULL)"
            " AND (key_type = 'client') = (client_type IS NOT NULL AND client_code IS NOT NULL)",
            name="owner",
        ),
        Index(
            "uq_mcp_api_key_user",
            "user_uuid",
            unique=True,
            postgresql_where=text("key_type = 'user'"),
        ),
        Index(
            "uq_mcp_api_key_client",
            "client_type",
            "client_code",
            unique=True,
            postgresql_where=text("key_type = 'client'"),
        ),
    )


class McpAudit(_Row, Base):
    __tablename__ = "mcp_audit"

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (Index("ix_mcp_audit_target_at", "target", "created_at"),)
