"""模型平台的表（agent-platform/doc/design/model-gateway-design.html）。

    gw_model       模型：对外的名字、类型、能力、价格、是否启用
    gw_account     账号：对接一个供应商接口的凭据（地址、加密的密钥、出网方式）
    gw_route       线路：模型 → 某个账号里的某个上游模型（优先级、权重）
    gw_api_key     API Key：用户 Key（本人申请）与客户端 Key（应用创建，管理员分配模型）
    gw_model_call  调用记录：网关每次调用一条，只追加，按月分区
    gw_audit       平台自己的操作日志（不记 Key 原文）

★ 全部是租户表（TenantOwned）：查询按当前租户自动过滤（atlas_tenancy）。

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。不建外键。
★ 表名带 gw_ 前缀：model_* 和 pydantic 的保留前缀撞名，ORM 类名也就不能叫 Model。
★ 模型名 = 调用时传的 model = 用户中心里的数据标识：租户内不能重名、不能改。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
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

VISIBILITIES = ("public", "private")
MODEL_KINDS = ("chat", "embedding", "rerank")
EGRESS = ("direct", "proxy")
KEY_TYPES = ("user", "client")
KEY_STATUSES = ("pending", "active", "blocked")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Row:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid7, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class GwModel(TenantOwned, _Row, Base):
    __tablename__ = "gw_model"

    #: 模型名：调用时传的 model；租户内唯一，不能改
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    #: chat / embedding / rerank
    kind: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    #: 停用：调用立即报「模型已停用」，Key 里保留
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 对调用方承诺的能力（kernel.ModelCaps）：看图、工具、思考、上下文、最大输出
    caps: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    #: 对内统一价（kernel.Price）：元 / 百万 token；空 = 不计费
    price: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    #: public = 所有人可选，不判权；private = 授权给用户或部门后才能选
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    __table_args__ = (
        CheckConstraint("visibility IN ('public','private')", name="visibility"),
        CheckConstraint("kind IN ('chat','embedding','rerank')", name="kind"),
        UniqueConstraint("tenant_uuid", "name", name="uq_gw_model_tenant_uuid_name"),
    )


class GwAccount(TenantOwned, _Row, Base):
    """账号：对接一个供应商接口的凭据。一个供应商可以有多个账号（公司账号、备用账号）。"""

    __tablename__ = "gw_account"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: 供应商（provider.spec.PROVIDERS 的名字）
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 按格式登记的地址 {"anthropic": url, "openai": url}：两种都有时挑和入口格式相同的
    endpoints: Mapped[dict[str, str]] = mapped_column(JSONType, default=dict, nullable=False)
    #: 信封加密后的密钥（secrets.SecretCipher）；自部署可以没有
    secret_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 密钥末 4 位（界面只显示它）
    secret_tail: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    #: direct / proxy（海外供应商经出网代理）
    egress: Mapped[str] = mapped_column(String(16), default="direct", nullable=False)
    #: 等第一段内容的超时（秒）；空 = 网关默认
    first_content_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 余额低于它就提醒模型管理员；空 = 不提醒
    balance_alert: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 最近一次查到的余额（能查余额的供应商，各家币种原样）
    balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    balance_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 最近一次检查（测试连接、定期检查）的时间和失败原因（成功为空）
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    check_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    __table_args__ = (
        CheckConstraint("egress IN ('direct','proxy')", name="egress"),
        UniqueConstraint("tenant_uuid", "name", name="uq_gw_account_tenant_uuid_name"),
    )


class GwRoute(TenantOwned, _Row, Base):
    """线路：模型 → 某个账号里的某个上游模型。先用优先级数字小的，同一优先级按权重分会话。"""

    __tablename__ = "gw_route"

    model_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    account_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: 供应商那边真实的模型名
    upstream_model: Mapped[str] = mapped_column(String(200), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        CheckConstraint("weight > 0", name="weight"),
        UniqueConstraint("model_uuid", "account_uuid", "upstream_model", name="uq_gw_route_target"),
    )


class GwApiKey(TenantOwned, _Row, Base):
    """一把 API Key：调用凭据，决定能用哪些模型、限额多少。

    用户 Key：本人建，一人可以多把（Cursor、Claude Code、脚本各一把，用 label 区分）；
              从「公有 + 授权给自己或所在部门的私有」里选模型。
              只存摘要，原文只在创建、重置时给一次。
    客户端 Key：client_type + client_code 在租户内唯一，应用调接口创建或管理员在页面上建，管理员分配
              模型、不看授权。原文加密保存（应用要取原文，crypto.SecretCipher）。

    status   pending  客户端 Key 刚创建、还没分配模型
             active   正常
             blocked  管理员（或本人）停用
    ★ models 存勾选的；effective_models 是管理平台按授权、模型是否存在和启用算好的「此刻能用的」，
      网关只认它。授权收回、模型删除时重算；失效的在页面标出来，下次保存时移除。
    """

    __tablename__ = "gw_api_key"

    key_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 用户 Key 的持有人（用户中心 uid）
    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: 客户端类型（Agent、Service ……）与编码（智能体 id ……）
    client_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 展示用的名字：用户 Key 是持有人姓名，客户端 Key 是对象名称（随对象改名同步）
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    #: 一把 Key 的用途名（Cursor、Claude Code……）
    label: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    #: 勾选的模型（模型名）
    models: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    #: 此刻能用的模型（网关只认它）
    effective_models: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 客户端 Key：加密后的原文（v1:…）；旧数据可能是明文，命令行 encrypt-secrets 一次性转掉。
    #: 用户 Key：空（只存摘要）
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 原文的摘要（sha256 十六进制）：网关只按它认 Key，内存里没有原文
    secret_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 原文末 4 位（界面上显示）
    secret_tail: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    #: 客户端 Key 重置后，旧的再保留到 prev_until（正在跑的会话不断）
    prev_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    prev_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 限额，都可以不设：月预算（元，软上限）、每分钟请求数、有效期
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 停用的原因（持有人在「我的 Key」里能看到）
    block_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: 最近一次生成原文的时间（生成、重置）
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("key_type IN ('user','client')", name="key_type"),
        CheckConstraint("status IN ('pending','active','blocked')", name="status"),
        CheckConstraint(
            "(key_type = 'user' AND user_uuid IS NOT NULL AND client_type IS NULL) OR "
            "(key_type = 'client' AND user_uuid IS NULL AND client_type IS NOT NULL "
            "AND client_code IS NOT NULL)",
            name="owner",
        ),
        Index("ix_gw_api_key_user", "user_uuid", postgresql_where=text("key_type = 'user'")),
        Index(
            "uq_gw_api_key_client",
            "tenant_uuid",
            "client_type",
            "client_code",
            unique=True,
            postgresql_where=text("key_type = 'client'"),
        ),
        Index(
            "uq_gw_api_key_digest",
            "secret_digest",
            unique=True,
            postgresql_where=text("secret_digest IS NOT NULL"),
        ),
    )


class GwAudit(TenantOwned, _Row, Base):
    __tablename__ = "gw_audit"

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (Index("ix_gw_audit_target_at", "target", "created_at"),)


class GwModelCall(TenantOwned, Base):
    """调用记录：一次调用一条，只追加、不修改；按 started_at 按月分区（分区由 metering 建）。

    不存请求和回答的内容。主键带分区键（PostgreSQL 分区表的要求）。
    """

    __tablename__ = "gw_model_call"

    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    call_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    key_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    key_kind: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    session_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entry_format: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ok / error / interrupted
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 每次尝试：route_uuid、account_uuid、上游模型、直通或转换、结果、耗时
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)
    first_content_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_hit_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 单价快照：input、cache_hit、output（元 / 百万 token）
    price: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    #: 转换时去掉的字段、参数整理的说明
    notes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("id", "started_at", name="pk_gw_model_call"),
        Index("uq_gw_model_call_uuid", "call_uuid", "started_at", unique=True),
        Index("ix_gw_model_call_tenant_time", "tenant_uuid", "started_at"),
        Index("ix_gw_model_call_key_time", "tenant_uuid", "key_uuid", "started_at"),
        Index("ix_gw_model_call_model_time", "tenant_uuid", "model", "started_at"),
        {"postgresql_partition_by": "RANGE (started_at)"},
    )
