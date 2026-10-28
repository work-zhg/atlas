"""管理接口与内部接口的请求、响应形状。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Visibility = Literal["public", "private"]


# ── 模型


class GrantIn(BaseModel):
    subject_type: Literal["user", "dept"]
    subject_id: str = Field(min_length=1, max_length=64)
    include_sub: bool = False


class RouteIn(BaseModel):
    """只交给 LiteLLM 的部分。"""

    api_base: str | None = Field(default=None, max_length=500)
    #: 供应商凭据。编辑时留空 = 不改
    api_key: str | None = Field(default=None, max_length=500)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    #: 美元 / 百万 token
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)


class ModelIn(RouteIn):
    name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=100)
    provider: str = Field(min_length=1, max_length=64)
    visibility: Visibility = "private"
    description: str = Field(default="", max_length=2000)
    #: 私有模型登记时一并授权
    grants: list[GrantIn] = Field(default_factory=list)


class ModelUpdate(RouteIn):
    display_name: str = Field(default="", max_length=100)
    provider: str = Field(min_length=1, max_length=64)
    visibility: Visibility
    description: str = Field(default="", max_length=2000)


class ModelReregister(RouteIn):
    provider: str = Field(min_length=1, max_length=64)


class ModelAdopt(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=100)
    visibility: Visibility = "private"
    description: str = Field(default="", max_length=2000)
    grants: list[GrantIn] = Field(default_factory=list)


class ConnectionTestIn(RouteIn):
    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)


class ConnectionTestOut(BaseModel):
    ok: bool
    message: str


class GrantOut(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    subject_name: str = ""
    permission: str
    include_sub: bool = False


class ModelOut(BaseModel):
    #: 平台记录的 id；LiteLLM 直配的为空
    uuid: UUID | None
    name: str
    display_name: str
    provider: str
    #: LiteLLM 直配的为空
    visibility: Visibility | None
    description: str = ""
    #: platform = 平台登记；litellm = LiteLLM 里直接配置的（只读，可纳入）
    source: Literal["platform", "litellm"]
    #: ok = 可调用；missing = LiteLLM 中缺失；unknown = LiteLLM 不可达，状态未知
    status: Literal["ok", "missing", "unknown"]
    #: 平台在 LiteLLM 里建的部署（能改地址、凭据、规格价格）；纳入平台的为 False
    managed: bool = False
    #: 直配模型的来源：db = LiteLLM 管理界面；config = 配置文件
    origin: Literal["db", "config"] | None = None
    deployments: int = 0
    api_base: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    #: 美元 / 百万 token
    input_price: float | None = None
    output_price: float | None = None
    #: 管理员看：私有模型的授权
    grants: list[GrantOut] | None = None
    #: 普通用户看：权限来源（public = 公有；granted = 授权给了你或你所在的部门）
    access: Literal["public", "granted"] | None = None
    created_by: str | None = None
    created_at: datetime | None = None


class ModelListOut(BaseModel):
    data: list[ModelOut]
    litellm_reachable: bool


class ProviderOut(BaseModel):
    code: str
    name: str
    needs_base: bool = False
    key_optional: bool = False


# ── Key


class KeyModelOut(BaseModel):
    name: str
    #: ok；revoked = 授权已撤销（或不再可选）；missing = LiteLLM 中缺失；deleted = 模型已删除
    state: Literal["ok", "revoked", "missing", "deleted"]


class KeyOut(BaseModel):
    uuid: UUID
    key_type: Literal["user", "client"]
    user_uuid: UUID | None = None
    client_type: str | None = None
    client_code: str | None = None
    name: str
    status: Literal["pending", "active", "blocked"]
    #: sk-…后四位；待分配时为空
    masked: str | None = None
    models: list[KeyModelOut]
    #: 有效模型：同步给 LiteLLM 的
    effective: list[str]
    #: 已分配过，但有效模型算下来为空（LiteLLM 里停用）
    no_models: bool = False
    sync_error: str | None = None
    created_by: str
    created_at: datetime
    rotated_at: datetime | None = None


class KeySecretOut(KeyOut):
    #: 原文：只在申请、重置、第一次分配时返回一次
    secret: str | None = None


class ModelsIn(BaseModel):
    models: list[str] = Field(min_length=1)


class ClientKeyIn(BaseModel):
    client_type: str = Field(min_length=1, max_length=64)
    client_code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=200)


class ClientTypeOut(BaseModel):
    code: str
    apps: list[str]


# ── 内部接口（应用）


class InternalModelOut(BaseModel):
    name: str
    display_name: str
    provider: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


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
