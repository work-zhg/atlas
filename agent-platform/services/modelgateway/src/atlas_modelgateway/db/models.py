"""模型网关的表（doc/model-design.html §4、§5、§6、§10）。

    gw_model         模型目录：平台模型名、类型、上游、协议、能力、上限、合规、生命周期；
                     接入检查的最近结果
    gw_model_price   价目：每次改价新增一行（带生效时间），成本按请求时生效的那一行算（§10.2）
    gw_model_probe   能力探测的结果：管理员确认后才写进目录（§4.4）
    gw_purpose       平台用途的配置：默认模型 + 允许范围（§6.7）
    gw_model_key     主体 Key：一个主体（人或数字员工）最多一把，原文存库，自动生成（§6.5）
    gw_model_call    每个经过网关的请求一行（含被拒的）：兼作访问日志和用量记录（§10.1）
    gw_budget        供应商额度：按「登记的额度 − 估算花费」预警（§10.3）
    gw_audit         管理动作的审计（不记 Key）

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。
★ 不建外键（与其它服务一致）：引用完整性由 service 层保证。
★ 表名带 gw_ 前缀：model_* 和 pydantic 的保留前缀撞名，ORM 类名也就不能叫 Model。
★ 模型没有删除：平台模型名是请求里的模型名、Agent 配置里的引用、用户中心里的数据标识，
  下线后也不能复用（§4.2）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow, uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

KEY_STATUSES = ("active", "disabled")
#: 价格的精度：每百万 token 的单价
PRICE = Numeric(18, 6)


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

    #: 平台模型名（catalog.MODEL_NAME_PATTERN）
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 提供方（DeepSeek、通义……）：展示，以及额度按它归集
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    #: chat | embedding | rerank
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # ── 上游（§9.5）：本期只有 Higress 上的 AI 路由
    upstream_kind: Mapped[str] = mapped_column(String(16), default="higress", nullable=False)
    #: 上游认的名字：转发时请求里的模型名换成它
    upstream_model: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 探测通过的调用协议，按偏好排序（原生的在前，§5.2）
    protocols: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    input_modalities: Mapped[list[str]] = mapped_column(
        JSONType, default=lambda: ["text"], nullable=False
    )
    #: catalog.Capabilities
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    # ── 上限
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 嵌入：向量维度；单批条数
    dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_batch: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── 合规与容量（只记录，不执行）
    data_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    capacity: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    # ── 生命周期（§5.3）
    status: Mapped[str] = mapped_column(String(16), default="onboarding", nullable=False)
    #: 紧急止血：和生命周期正交，恢复后回到原来的状态
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disabled_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 计划下线的时间：到了由维护任务下线
    retire_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 替代模型（平台模型名）
    replacement: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # ── 接入检查（§9.3）：不带网关凭据访问上游，必须被拒
    access_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: True = 不带凭据被拒（通过）；False = 不带凭据也能调通（漏开了认证）；None = 没查成
    access_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    access_check_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('chat','embedding','rerank')", name="kind"),
        CheckConstraint("upstream_kind IN ('higress')", name="upstream_kind"),
        CheckConstraint(
            "status IN ('onboarding','available','deprecated','retired')", name="status"
        ),
    )


class GwModelPrice(_Row, Base):
    """一版价目（每百万 token 的单价）。改价 = 新增一行，旧的留着：历史成本不随改价变（§10.2）。

    ★ 缓存读写价为空时按输入价算。分段价 tiers：[{above_input_tokens, input, output, …}]，
      一次请求的输入（含缓存）超过阈值就用那一段的单价（取阈值最大的那段）。
    """

    __tablename__ = "gw_model_price"

    model_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    input: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    output: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    cache_read: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    cache_write: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: 按次计价的部分（每次请求）
    per_request: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    tiers: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    __table_args__ = (Index("ix_gw_model_price_model_from", "model_uuid", "effective_from"),)


class GwModelProbe(_Row, Base):
    """一次能力探测（§4.4）。

    results：[{check, protocol, ok, detail}]；suggested：建议写进目录的属性。
    """

    __tablename__ = "gw_model_probe"

    model_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list, nullable=False)
    suggested: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    #: 和当前目录不一致的属性：{属性: [目录里的, 探测得出的]}
    differences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    applied_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_gw_model_probe_model_at", "model_uuid", "created_at"),)


class GwPurpose(_Row, Base):
    __tablename__ = "gw_purpose"

    #: catalog.PURPOSES 的键
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    default_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 允许范围（平台模型名）；默认模型总在范围内
    allowed_models: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class GwModelKey(_Row, Base):
    """主体 Key 只表示「我是谁」，不带权限：能用什么交给用户中心的数据权限（§6.5）。

    ★ 自动生成：第一次被取用时生成（issued_by = system），不需要手工发放。
    ★ 原文存库：Atlas 代主体调用时要带原文，不能只存哈希。加密等安全问题后续统一设计。
    ★ 不提供删除：只停用，审计记录永远能对上持有人。启用时重新生成，不恢复旧值。
    """

    __tablename__ = "gw_model_key"

    subject_uid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False)
    #: human | digital（「数字员工的 Key 永远不显示」用）
    subject_type: Mapped[str] = mapped_column(String(16), default="human", nullable=False)
    subject_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    api_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    issued_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 最近一次生成原文的时间（生成、重置、启用）
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="status"),)


class GwModelCall(_Row, Base):
    """一个经过网关的请求（§10.1）：所有调用都有，包括 Pod 里的 CLI、平台功能和泄露 Key 的调用。

    可信的：主体（来自 Key）、应用与用途、模型、用量、结果、耗时。
    只作参考的：使用者、Agent、Run（来自调用方的请求头）。
    ★ 不记提示词和输出内容。
    """

    __tablename__ = "gw_model_call"

    #: 请求里的平台模型名（解析不出来时为空串）
    model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    upstream_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: messages | count_tokens | chat | embeddings | rerank | models
    endpoint: Mapped[str] = mapped_column(String(16), nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Key 认出的权限主体。平台用途、没 Key、Key 无效时为空
    subject_uid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: 请求时主体所在的部门（部门调整不改历史）
    dept_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caller_app: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ok = 已转发且上游成功；其余是拒绝原因（proxy.Reason）或 upstream_error
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    forwarded: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # ── 用量（归一化：input 是不含缓存的输入，output 含推理）
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 上游回报了用量。False = 没回报（成本未知，§10.2）
    usage_reported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)

    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_gw_model_call_at", "created_at"),
        Index("ix_gw_model_call_model_at", "model", "created_at"),
        Index("ix_gw_model_call_subject_at", "subject_uid", "created_at"),
        Index("ix_gw_model_call_purpose_at", "purpose", "created_at"),
    )


class GwBudget(_Row, Base):
    """供应商额度（§10.3）。网关不持有供应商 Key、查不了余额：剩余 = 额度 − 自 starts_at 以来
    这个提供方的模型估算花费；低于 warn_ratio 就预警。"""

    __tablename__ = "gw_budget"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: 归集到哪个提供方（gw_model.provider）
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: 剩余低于额度的这个比例时预警
    warn_ratio: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class GwAudit(_Row, Base):
    __tablename__ = "gw_audit"

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (Index("ix_gw_audit_target_at", "target", "created_at"),)
