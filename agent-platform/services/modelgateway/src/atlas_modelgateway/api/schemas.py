"""管理接口与内部接口的请求 / 响应。

★ 内部接口（/internal/*）是给 Atlas、知识库的契约：Atlas 侧有一份对应的解析
  （atlas_agent/providers/llm/modelgateway.py），改字段要两边一起改。
★ 管理接口不返回 Key 原文（§6.5）：只有内部接口 /internal/subjects/{uid}/access 返回原文。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ..catalog import MODEL_NAME_PATTERN

# ───────────────────────────── 模型


class _Attributes(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    provider: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = None
    #: 上游认的名字；缺省 = 平台模型名
    upstream_model: str | None = Field(default=None, min_length=1, max_length=128)
    #: 调用协议，按偏好排序（原生的在前）
    protocols: list[Literal["anthropic", "openai", "rerank"]] | None = None
    input_modalities: list[str] | None = None
    #: catalog.Capabilities
    capabilities: dict[str, Any] | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    dimensions: int | None = Field(default=None, gt=0)
    max_batch: int | None = Field(default=None, gt=0)
    #: catalog.DataPolicy
    data_policy: dict[str, Any] | None = None
    #: catalog.Capacity
    capacity: dict[str, Any] | None = None


class ModelIn(_Attributes):
    name: str = Field(pattern=MODEL_NAME_PATTERN)
    kind: Literal["chat", "embedding", "rerank"] = "chat"


class ModelPatch(_Attributes):
    pass


class AccessCheckOut(BaseModel):
    ok: bool | None
    detail: str | None
    checked_at: datetime | None


class PriceIn(BaseModel):
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    #: 每百万 token 的单价
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cache_read: Decimal | None = Field(default=None, ge=0)
    cache_write: Decimal | None = Field(default=None, ge=0)
    per_request: Decimal | None = Field(default=None, ge=0)
    #: [{above_input_tokens, input?, output?, cache_read?, cache_write?}]
    tiers: list[dict[str, Any]] = []
    #: 缺省 = 现在
    effective_from: datetime | None = None


class PriceOut(BaseModel):
    id: UUID
    currency: str
    input: Decimal
    output: Decimal
    cache_read: Decimal | None
    cache_write: Decimal | None
    per_request: Decimal | None
    tiers: list[dict[str, Any]]
    effective_from: datetime
    created_by: str
    created_at: datetime


class ModelOut(BaseModel):
    name: str
    display_name: str
    description: str
    provider: str
    tags: list[str]
    kind: str
    upstream_kind: str
    upstream_model: str
    protocols: list[str]
    input_modalities: list[str]
    capabilities: dict[str, Any]
    context_window: int | None
    max_output_tokens: int | None
    dimensions: int | None
    max_batch: int | None
    data_policy: dict[str, Any]
    capacity: dict[str, Any]
    status: str
    disabled: bool
    disabled_reason: str
    deprecated_at: datetime | None
    retire_at: datetime | None
    retired_at: datetime | None
    replacement: str | None
    access_check: AccessCheckOut
    #: 当前生效的价目；没有为空（成本显示为未知）
    price: PriceOut | None = None
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class DeprecateIn(BaseModel):
    replacement: str | None = None
    retire_at: datetime | None = None


class DisableIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class ProbeOut(BaseModel):
    id: UUID
    created_at: datetime
    created_by: str
    results: list[dict[str, Any]]
    suggested: dict[str, Any]
    differences: dict[str, Any]
    applied_by: str | None
    applied_at: datetime | None


# ───────────────────────────── 平台用途


class PurposeOut(BaseModel):
    code: str
    name: str
    kind: str
    description: str
    #: False = 不是调用，只是默认选项
    callable: bool
    default_model: str | None
    allowed_models: list[str]


class PurposeIn(BaseModel):
    default_model: str | None = None
    allowed_models: list[str] = []


# ───────────────────────────── 授权、Key


class SubjectRef(BaseModel):
    type: Literal["user", "dept"]
    id: UUID


class GrantIn(BaseModel):
    subjects: list[SubjectRef] = Field(min_length=1)
    level: Literal["READ", "OWNER"] = "READ"
    include_sub: bool = True


class GrantPatch(BaseModel):
    level: Literal["READ", "OWNER"] | None = None
    include_sub: bool | None = None


class KeyOut(BaseModel):
    id: UUID
    subject_uid: UUID
    subject_type: str
    subject_name: str
    #: 掩码。原文不经管理接口返回（数字员工的 Key 永远不显示）
    masked: str
    status: str
    issued_by: str
    created_at: datetime
    rotated_at: datetime
    disabled_at: datetime | None


# ───────────────────────────── 用量、额度


class CallOut(BaseModel):
    id: int
    at: datetime
    model: str
    upstream_model: str | None
    endpoint: str
    stream: bool
    subject_uid: UUID | None
    dept_id: str | None
    purpose: str | None
    caller_app: str | None
    user_uid: str | None
    agent_id: str | None
    run_id: str | None
    outcome: str
    status: int
    forwarded: bool
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    usage_reported: bool
    cost: Decimal | None
    currency: str | None
    ttft_ms: int | None
    duration_ms: int


UsageGroup = Literal["model", "subject", "dept", "purpose", "caller_app", "agent", "day"]


class UsageRowOut(BaseModel):
    #: 分组的值（day 是 YYYY-MM-DD；平台用途的调用在 subject 分组里为空）
    key: str | None
    currency: str | None
    calls: int
    failed: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    #: 已知价格的部分
    cost: Decimal
    #: 回报了用量但没有价格的调用数（成本未知，§10.2）
    unpriced: int
    #: 没有回报用量的成功调用数（用量未知）
    unreported: int


class BudgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    starts_at: datetime | None = None
    warn_ratio: float = Field(default=0.2, gt=0, lt=1)
    note: str = ""


class BudgetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    amount: Decimal | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    warn_ratio: float | None = Field(default=None, gt=0, lt=1)
    note: str | None = None
    active: bool | None = None


class BudgetOut(BaseModel):
    id: UUID
    name: str
    provider: str
    amount: Decimal
    currency: str
    starts_at: datetime
    warn_ratio: float
    note: str
    active: bool
    #: 自 starts_at 以来的估算花费、剩余、剩余比例
    spent: Decimal
    remaining: Decimal
    remaining_ratio: float
    #: 回报了用量但没有价格的调用数：花费被低估
    unpriced_calls: int
    warning: bool


class AlertOut(BaseModel):
    kind: str
    message: str
    target: str | None = None


# ───────────────────────────── 内部接口（给 Atlas、知识库）


class CatalogModelOut(BaseModel):
    name: str
    display_name: str
    description: str
    provider: str
    tags: list[str]
    kind: str
    protocols: list[str]
    input_modalities: list[str]
    capabilities: dict[str, Any]
    context_window: int | None
    max_output_tokens: int | None
    dimensions: int | None
    max_batch: int | None
    data_policy: dict[str, Any]
    status: str
    disabled: bool
    deprecated_at: datetime | None
    retire_at: datetime | None
    replacement: str | None
    #: 当前价目（编辑器显示价格档位）
    price: PriceOut | None = None


class CatalogOut(BaseModel):
    models: list[CatalogModelOut]
    purposes: list[PurposeOut]


class SubjectAccessOut(BaseModel):
    subject: str
    #: 主体此刻能用的对话模型（有「使用」、可用或弃用、没停用）
    models: list[str]
    all_models: bool
    #: 原文；主体不存在、已停用或 Key 被停用时为空
    key: str | None
    key_status: Literal["active", "disabled", "none"]
