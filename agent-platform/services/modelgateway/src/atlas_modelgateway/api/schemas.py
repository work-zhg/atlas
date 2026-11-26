"""管理接口、内部接口的请求和响应体。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ── 共用 ────────────────────────────────────────────────────────────────


class GrantIn(BaseModel):
    subject_type: Literal["user", "dept"]
    subject_id: str = Field(min_length=1, max_length=64)
    include_sub: bool = False


class GrantOut(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    subject_name: str = ""
    include_sub: bool = False
    permission: str = "READ"


class ProviderOut(BaseModel):
    code: str
    name: str
    #: 提供哪些格式，第一个是推荐的
    formats: list[str]
    #: 各格式的默认地址（空串 = 必须手填）
    endpoints: dict[str, str]
    can_balance: bool
    can_list_models: bool
    key_optional: bool
    needs_endpoint: bool
    overseas: bool
    hint: str = ""


# ── 账号 ────────────────────────────────────────────────────────────────


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=64)
    endpoints: dict[str, str]
    #: 新建时必填（自部署可以空）；修改时不传 = 不改
    secret: str | None = Field(default=None, max_length=4000)
    egress: Literal["direct", "proxy"] = "direct"
    first_content_timeout_s: int | None = Field(default=None, ge=10, le=3600)
    balance_alert: Decimal | None = Field(default=None, ge=0)
    remark: str = Field(default="", max_length=1000)
    enabled: bool = True


class AccountDraftTest(AccountIn):
    upstream_model: str | None = None


class AccountTestIn(BaseModel):
    upstream_model: str | None = None


class IssueOut(BaseModel):
    kind: str
    since: datetime
    message: str = ""


class AccountOut(BaseModel):
    id: UUID
    name: str
    provider: str
    provider_name: str
    endpoints: dict[str, str]
    secret_masked: str | None
    egress: str
    first_content_timeout_s: int | None
    balance_alert: Decimal | None
    balance: Decimal | None
    balance_at: datetime | None
    remark: str
    enabled: bool
    #: ok / cooling / abnormal / disabled
    status: str
    issue: IssueOut | None = None
    cooling_routes: int = 0
    low_balance: bool = False
    routes: int = 0
    checked_at: datetime | None
    check_error: str
    created_by: str
    created_at: datetime


class ProbeStepOut(BaseModel):
    name: str
    ok: bool
    message: str = ""


class ProbeOut(BaseModel):
    ok: bool
    steps: list[ProbeStepOut]
    upstream_models: list[str] | None = None
    balance: Decimal | None = None


# ── 模型 ────────────────────────────────────────────────────────────────


class CapsIO(BaseModel):
    vision: bool = False
    tools: bool = False
    thinking: Literal["none", "optional", "always_on"] = "none"
    context_window: int | None = Field(default=None, ge=1)
    max_output: int | None = Field(default=None, ge=1)


class PriceIO(BaseModel):
    """元 / 百万 token。都不填 = 不计费；cache_write 不填 = 按输入价算。"""

    input: Decimal = Field(default=Decimal(0), ge=0)
    cache_hit: Decimal = Field(default=Decimal(0), ge=0)
    output: Decimal = Field(default=Decimal(0), ge=0)
    cache_write: Decimal | None = Field(default=None, ge=0)


class RouteIn(BaseModel):
    account_id: UUID
    #: 不填就和模型名一样
    upstream_model: str = Field(default="", max_length=200)
    priority: int = Field(default=1, ge=0, le=100)
    weight: int = Field(default=1, ge=1, le=1000)


class ModelUpdate(BaseModel):
    display_name: str = Field(default="", max_length=100)
    visibility: Literal["public", "private"] = "private"
    description: str = Field(default="", max_length=2000)
    #: 不传 = 按第一条线路的预置带出（新建时）/ 不改（修改时）
    caps: CapsIO | None = None
    price: PriceIO = Field(default_factory=PriceIO)
    enabled: bool = True
    routes: list[RouteIn] = Field(min_length=1)


class ModelIn(ModelUpdate):
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["chat", "embedding", "rerank"] = "chat"
    grants: list[GrantIn] = Field(default_factory=list)


class RouteOut(BaseModel):
    id: UUID
    account_id: UUID
    account_name: str
    provider: str
    upstream_model: str
    priority: int
    weight: int
    #: ok / cooling / abnormal / disabled
    status: str


class ModelOut(BaseModel):
    id: UUID
    name: str
    display_name: str
    kind: str
    visibility: str
    description: str
    caps: CapsIO
    price: PriceIO
    enabled: bool
    #: available / degraded（首选不可用，走备用）/ unavailable / disabled
    status: str
    routes: list[RouteOut] = Field(default_factory=list)
    #: 普通用户看到的：public / granted
    access: str | None = None
    grants: list[GrantOut] | None = None
    #: 多少把 Key 勾选了它
    keys: int = 0


class CapsSuggestOut(BaseModel):
    caps: CapsIO
    preset: bool


# ── Key ─────────────────────────────────────────────────────────────────


class KeyIn(BaseModel):
    label: str = Field(default="", max_length=100)
    models: list[str] = Field(default_factory=list)
    monthly_budget: Decimal | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class KeyUpdate(BaseModel):
    """只改传了的字段（显式传 null = 清掉这个限额）。"""

    label: str | None = Field(default=None, max_length=100)
    models: list[str] | None = None
    monthly_budget: Decimal | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None


class ClientKeyIn(KeyIn):
    client_type: str = Field(min_length=1, max_length=64)
    client_code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=200)


class BlockIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class KeyModelOut(BaseModel):
    name: str
    display_name: str = ""
    #: ok / disabled（模型停用）/ revoked（授权已收回）/ missing（模型已删除）
    state: str


class KeyOut(BaseModel):
    id: UUID
    key_type: str
    label: str
    #: 用户 Key 是持有人，客户端 Key 是对象名称
    name: str
    user_id: UUID | None = None
    client_type: str | None = None
    client_code: str | None = None
    models: list[KeyModelOut]
    status: str
    block_reason: str = ""
    masked: str | None = None
    monthly_budget: Decimal | None = None
    rpm: int | None = None
    expires_at: datetime | None = None
    spent_this_month: Decimal = Decimal(0)
    last_used_at: datetime | None = None
    created_at: datetime
    rotated_at: datetime | None = None


class KeySecretOut(KeyOut):
    #: 原文：只在创建、重置时给这一次
    secret: str | None = None


class ClientTypeOut(BaseModel):
    code: str
    apps: list[str]
    default_public: bool


# ── 调用记录 ────────────────────────────────────────────────────────────


class CallOut(BaseModel):
    id: UUID
    started_at: datetime
    model: str
    key_id: UUID | None
    key_kind: str
    session: str | None
    entry_format: str
    status: str
    error_category: str | None
    attempts: list[dict[str, Any]]
    first_content_ms: int | None
    duration_ms: int
    input_tokens: int
    cache_write_tokens: int
    cache_hit_tokens: int
    output_tokens: int
    estimated: bool
    cost: Decimal
    notes: dict[str, Any]


class CallsPage(BaseModel):
    data: list[CallOut]
    #: 下一页传 before=它；空 = 没有了
    next_before: datetime | None = None


class CallSummaryOut(BaseModel):
    group: str
    label: str = ""
    calls: int
    errors: int
    input: int
    cache_write: int
    cache_hit: int
    output: int
    cost: Decimal


# ── 内部接口（应用） ────────────────────────────────────────────────────


class InternalModelOut(BaseModel):
    name: str
    display_name: str
    kind: str = "chat"
    #: 第一条线路的供应商（只作展示）
    provider: str = ""
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool = False
    capabilities: CapsIO = Field(default_factory=CapsIO)


class InternalClientKeyOut(BaseModel):
    client_type: str
    client_code: str
    name: str
    #: pending_models = 待分配；active；blocked = 停用；no_models = 分配的模型都不可用了
    status: Literal["pending_models", "active", "blocked", "no_models"]
    #: 原文：只有 active 时给
    key: str | None = None
    models: list[InternalModelOut] = Field(default_factory=list)


class InternalCatalogOut(BaseModel):
    """已登记、可调用的全部模型（客户端 Key 还没分配时，编辑器先列它们）。"""

    models: list[InternalModelOut]
