"""模型管理平台的表（模型管理方案设计第 3、5、6 章）。

    gw_model     平台登记的模型：只落基本信息。地址、供应商凭据、规格价格在 LiteLLM
    gw_api_key   API Key：用户 Key（本人申请）与客户端 Key（应用创建，管理员分配模型）
    gw_audit     平台自己的操作日志（不记 Key 原文）

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。不建外键。
★ 表名带 gw_ 前缀：model_* 和 pydantic 的保留前缀撞名，ORM 类名也就不能叫 Model。
★ 模型名 = LiteLLM 的 model_name = 调用时传的 model = 用户中心里的数据标识，所以不能改、不能重名。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Index, MetaData, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow, uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

VISIBILITIES = ("public", "private")
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


class GwModel(_Row, Base):
    __tablename__ = "gw_model"

    #: 模型名：供应商侧的模型名，同时是 LiteLLM 的 model_name 和调用时传的 model
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    #: 供应商 = LiteLLM 的 custom_llm_provider
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    #: public = 所有人可选，不判权；private = 授权给用户或部门后才能选
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 平台在 LiteLLM 里建的部署（= 本行 uuid）。None = 纳入平台的 LiteLLM 直配模型，平台不改它
    litellm_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    __table_args__ = (CheckConstraint("visibility IN ('public','private')", name="visibility"),)


class GwApiKey(_Row, Base):
    """一把 API Key。

    用户 Key：一人一把（user_uuid 唯一），本人从「公有 + 授权给自己的私有」里选模型。
    客户端 Key：client_type + client_code 唯一，应用调接口创建或管理员在页面上建，
               管理员从全部已登记模型里分配。

    status   pending  客户端 Key 刚创建、还没分配模型：LiteLLM 里还没有 Key（空列表 = 全部模型）
             active   正常
             blocked  管理员（或本人）停用
    ★ 「无可用模型」不是一种状态：有效模型算下来为空时，LiteLLM 里停用这把 Key，恢复后自动解除。
    ★ 原文存库：应用要取原文去调 LiteLLM，LiteLLM 只存哈希。加密等安全问题后续统一设计。
    ★ models 存勾选的；失效的（授权撤销、LiteLLM 中缺失）先保留并在页面标出来，下次保存时移除。
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
    #: 分配的模型（模型名）
    models: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 原文。pending 时为空
    secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: LiteLLM 里的标识（哈希）
    litellm_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ── 同步到 LiteLLM 的状态（平台是事实源，LiteLLM 跟随）
    #: 上一次成功推给 LiteLLM 的可用模型与停用状态
    synced_models: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    synced_blocked: Mapped[bool | None] = mapped_column(nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 上一次同步失败的原因；成功后清空。对账任务据此重试
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        Index(
            "uq_gw_api_key_user",
            "user_uuid",
            unique=True,
            postgresql_where=text("key_type = 'user'"),
        ),
        Index(
            "uq_gw_api_key_client",
            "client_type",
            "client_code",
            unique=True,
            postgresql_where=text("key_type = 'client'"),
        ),
    )


class GwAudit(_Row, Base):
    __tablename__ = "gw_audit"

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (Index("ix_gw_audit_target_at", "target", "created_at"),)
