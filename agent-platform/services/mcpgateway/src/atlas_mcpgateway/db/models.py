"""MCP 网关的表（doc/mcp-design.html §3、§4、§5.5、§10）。

    mcp_service      服务注册表：服务名、上游、凭据方式、是否要求复核、超时、状态；
                     元数据刷新与接入检查的最近结果
    mcp_tool         工具的当前定义（从上游拉来存库）。保留变化前的一版；上游删掉的工具留着行、
                     记下删除时间（管理端据此显示新增 / 删除 / 变化）
    mcp_tool_review  复核结论，按「服务 + 工具 + 定义指纹」记：批准的是某一版定义，不是工具本身
    mcp_api_key      API Key：一个主体（人或数字员工）最多一把，原文存库（§5.5）
    mcp_access_log   数据面的访问日志：每个经过网关的请求一行，含被拒的
    mcp_audit        管理动作的审计（不记 Key）

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。
★ 不建外键（与其它服务一致）：引用完整性由 service 层保证。
★ 服务没有删除：服务名是工具名前缀、用户中心里的数据标识、Agent 配置里的引用，
  停用后也不能复用（§3.2）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow, uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

#: 上游类型（§3.2「上游是替换 Higress 的接缝」）。本期只有 Higress 上的服务
UPSTREAM_KINDS = ("higress",)
#: 凭据方式（§5.6）。user_oauth = 用户的第三方授权，远期
CREDENTIAL_MODES = ("subject_key", "user_oauth")
SERVICE_STATUSES = ("enabled", "disabled")
REVIEW_DECISIONS = ("approved", "rejected")
KEY_STATUSES = ("active", "disabled")


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
    upstream_kind: Mapped[str] = mapped_column(String(16), default="higress", nullable=False)
    #: 上游里的名字：Higress 上 MCP 服务的名字（可以和服务名不同）
    upstream_name: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_mode: Mapped[str] = mapped_column(String(16), default="subject_key", nullable=False)
    review_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 单次 tools/call 的超时（秒）。None = 调用方的全局默认。由调用方（Atlas）执行
    call_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="enabled", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # ── 元数据刷新（§4.1）
    #: 最近一次拉取的时间（成功或失败都推进，失败时 fetch_error 非空、工具定义保持旧的）
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 全部在册工具指纹的哈希：任何一个工具的任何字段变了，它就变
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: content_hash 最近一次变化的时间
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 多副本只让一个去刷新：领到刷新的时间（租约，见 registry.claim_refresh）
    refresh_claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # ── 接入检查（§8.3）：不带网关凭据访问上游，必须被拒
    access_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: True = 不带凭据被拒（通过）；False = 不带凭据也能调通（漏开了认证）；None = 没查成
    access_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    access_check_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("upstream_kind IN ('higress')", name="upstream_kind"),
        CheckConstraint("credential_mode IN ('subject_key','user_oauth')", name="credential_mode"),
        CheckConstraint("status IN ('enabled','disabled')", name="status"),
    )


class McpTool(_Row, Base):
    __tablename__ = "mcp_tool"

    service_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: 发给服务的名字（线上名）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: MCP 线上格式的注解（readOnlyHint …）
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 定义指纹（definitions.digest_of）
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 机械检查不通过的原因。非空 = 不可用（§4.2）
    issues: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    #: 变化前的一版：{description, input_schema, annotations, digest}
    previous: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 上游最近一次完整拉取里没有它 = 已删除。留着行：管理端要显示「删除了哪些」
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (UniqueConstraint("service_uuid", "name"),)


class McpToolReview(_Row, Base):
    __tablename__ = "mcp_tool_review"

    service_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("service_uuid", "tool_name", "digest"),
        CheckConstraint("decision IN ('approved','rejected')", name="decision"),
    )


class McpApiKey(_Row, Base):
    """API Key 只表示「我是谁」，不带权限：能用什么交给用户中心的数据权限（§5.5）。

    ★ 原文存库：Atlas 代主体调用时要带原文，不能只存哈希。存储加密等安全问题后续统一设计。
    ★ 不提供删除：只停用，审计记录永远能对上持有人。启用时重新生成，不恢复旧值。
    """

    __tablename__ = "mcp_api_key"

    #: 持有人：用户中心的用户（人或数字员工）
    subject_uid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False)
    #: human | digital（发放时从用户中心取，展示与「数字员工的 Key 永远不显示」用）
    subject_type: Mapped[str] = mapped_column(String(16), default="human", nullable=False)
    subject_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    api_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    issued_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 最近一次生成原文的时间（发放、重置、启用）
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="status"),)


class McpAccessLog(_Row, Base):
    """数据面的访问日志（§10）：所有经过网关的请求都有，包括 Pod 里的 CLI 和泄露 Key 的调用。"""

    __tablename__ = "mcp_access_log"

    service: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Key 认出的权限主体。没 Key / Key 无效时为空
    subject_uid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: 调用方自带的使用者（X-Uid），只作审计
    user_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caller_app: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_method: Mapped[str] = mapped_column(String(8), nullable=False)
    #: JSON-RPC 方法（initialize、tools/list、tools/call …）；GET 推送流、DELETE 为空
    rpc_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: ok = 已转发；其余是拒绝原因（proxy.Reason）
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 有没有转发给上游
    forwarded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_mcp_access_log_service_at", "service", "created_at"),
        Index("ix_mcp_access_log_subject_at", "subject_uid", "created_at"),
    )


class McpAudit(_Row, Base):
    __tablename__ = "mcp_audit"

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (Index("ix_mcp_audit_target_at", "target", "created_at"),)
