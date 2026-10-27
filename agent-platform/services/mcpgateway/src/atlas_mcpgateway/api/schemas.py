"""管理接口与内部接口的请求 / 响应。

★ 内部接口（/internal/*）是给 Atlas 的契约：Atlas 侧有一份对应的解析（providers/mcp/gateway.py），
  改字段要两边一起改。
★ 管理接口不返回 Key 原文（§5.5）：只有内部接口 /internal/subjects/{uid}/access 返回原文。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ..definitions import SERVICE_NAME_PATTERN

# ───────────────────────────── 服务


class ServiceIn(BaseModel):
    name: str = Field(pattern=SERVICE_NAME_PATTERN)
    display_name: str = Field(default="", max_length=100)
    description: str = ""
    #: Higress 上 MCP 服务的名字；缺省 = 服务名
    upstream_name: str | None = Field(default=None, min_length=1, max_length=128)
    review_required: bool = True
    call_timeout_s: float | None = Field(default=None, gt=0)
    #: 本期只有主体 Key（§5.6）；用户的第三方授权是远期
    credential_mode: Literal["subject_key"] = "subject_key"


class ServicePatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    upstream_name: str | None = Field(default=None, min_length=1, max_length=128)
    review_required: bool | None = None
    #: 显式给 null = 清掉，改用调用方的全局默认
    call_timeout_s: float | None = Field(default=None, gt=0)


class AccessCheckOut(BaseModel):
    ok: bool | None
    detail: str | None
    checked_at: datetime | None


class ServiceOut(BaseModel):
    name: str
    display_name: str
    description: str
    upstream_kind: str
    upstream_name: str
    credential_mode: str
    review_required: bool
    call_timeout_s: float | None
    enabled: bool
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    fetched_at: datetime | None
    fetch_error: str | None
    changed_at: datetime | None
    content_hash: str | None
    access_check: AccessCheckOut
    #: 各复核状态的工具个数（ok / pending_review / rejected / invalid）
    tool_counts: dict[str, int] = {}


class ReviewOut(BaseModel):
    decision: str
    comment: str
    reviewed_by: str
    reviewed_at: datetime


class ToolOut(BaseModel):
    name: str
    #: 模型看到的名字（服务名__工具名）；线上名不合规时为空
    model_name: str | None
    description: str
    input_schema: dict[str, Any]
    #: MCP 线上格式（readOnlyHint …）
    annotations: dict[str, Any] | None
    read_only: bool
    digest: str
    issues: list[str]
    #: ok = 能选、能调；invalid = 机械检查不通过；pending_review；rejected
    status: str
    changed_at: datetime | None
    #: 变化前的一版：{description, input_schema, annotations, digest}
    previous: dict[str, Any] | None
    review: ReviewOut | None = None


class ServiceDetailOut(ServiceOut):
    tools: list[ToolOut] = []
    #: 上游已经删掉的工具
    removed_tools: list[str] = []


class ReviewIn(BaseModel):
    #: 复核的是哪一版定义：必须是当前指纹
    digest: str = Field(min_length=64, max_length=64)
    decision: Literal["approved", "rejected"]
    comment: str = ""


class PendingReviewOut(BaseModel):
    service: str
    tool: ToolOut


# ───────────────────────────── 授权


class SubjectIn(BaseModel):
    #: user 包括数字员工
    type: Literal["user", "dept"]
    id: UUID


class GrantIn(BaseModel):
    """与知识库共享同一种形状：一次授给多个人 / 部门。"""

    #: READ = 使用；OWNER = 所有者（能授权）
    level: Literal["READ", "OWNER"] = "READ"
    subjects: list[SubjectIn] = Field(min_length=1)
    #: 授给部门时含下级
    include_sub: bool = True


class GrantPatch(BaseModel):
    level: Literal["READ", "OWNER"] | None = None
    include_sub: bool | None = None


# ───────────────────────────── API Key


class KeyIn(BaseModel):
    subject_uid: UUID
    #: 同时给持有人授这些服务的「使用」（写的是持有人的服务授权，不是 Key 的权限）
    services: list[str] = []


class KeyOut(BaseModel):
    id: UUID
    subject_uid: UUID
    subject_type: str
    subject_name: str
    #: 掩码。★ 不返回原文
    masked: str
    status: str
    issued_by: str
    created_at: datetime
    rotated_at: datetime
    disabled_at: datetime | None


class MissingKeyOut(BaseModel):
    """有授权但没有 Key 的人（§5.5）：管理端据此批量发放。"""

    subject_uid: str
    name: str
    services: list[str]


# ───────────────────────────── 访问日志


class AccessLogOut(BaseModel):
    id: int
    at: datetime
    service: str
    subject_uid: UUID | None
    user_uid: str | None
    caller_app: str | None
    run_id: str | None
    http_method: str
    rpc_method: str | None
    tool: str | None
    outcome: str
    status: int
    forwarded: bool
    duration_ms: int


# ───────────────────────────── 内部接口（给 Atlas）


class MetadataServiceOut(BaseModel):
    name: str
    display_name: str
    description: str
    enabled: bool
    review_required: bool
    call_timeout_s: float | None
    credential_mode: str
    fetched_at: datetime | None
    fetch_error: str | None
    changed_at: datetime | None
    content_hash: str | None
    tools: list[ToolOut]
    removed_tools: list[str] = []


class MetadataOut(BaseModel):
    services: list[MetadataServiceOut]


class SubjectAccessOut(BaseModel):
    """一个主体此刻能用什么（demo F5：可用服务和 Key 一次给出）。"""

    subject: str
    #: 能用（≥ 使用）且已启用的服务
    services: list[str]
    #: 有「全部 MCP 服务」这类编码级授权
    all_services: bool
    #: 原文；没有或已停用为 None。★ 只有这个内部接口返回原文
    key: str | None
    key_status: Literal["active", "disabled", "none"]
