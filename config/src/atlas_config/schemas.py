"""配置服务的对外契约。

★ 这是 atlas_server **唯一**允许 import 的模块（import-linter）。所以它只依赖
  pydantic：import 它不该把 sqlalchemy / boto3 / fastapi 带进调用方。
★ 运行时用的模型（SkillVersionOut 等）字段只增不改；新增字段必须有默认值 ——
  运行时把它们永久缓存在磁盘上，旧缓存要能被新代码读回来。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "SENSITIVE_HEADER",
    "SKILL_SLUG_PATTERN",
    "AuditOut",
    "McpServerIn",
    "McpServerOut",
    "McpServerPatch",
    "ReviewDecisionIn",
    "RevokedSkill",
    "SkillCatalogItem",
    "SkillDetailOut",
    "SkillFile",
    "SkillStatus",
    "SkillVersionAdminOut",
    "SkillVersionBrief",
    "SkillVersionOut",
    "StatusChangeIn",
    "ToolReviewIn",
    "ToolReviewOut",
]

SKILL_SLUG_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
#: 与运行时 domain/mcp_naming.SERVER_NAME_PATTERN 一致：不含 `_`（模型侧名以 `__` 分隔）
MCP_SERVER_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,23}$"
#: 这些 header 的值必须含 ${ENV} 占位符 —— 从结构上拒绝把真凭据存进配置库
SENSITIVE_HEADER = re.compile(
    r"(?i)^(authorization|proxy-authorization|cookie|.*(key|token|secret).*)$"
)
_ENV_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

SkillStatus = Literal["pending_review", "rejected", "published", "disabled", "revoked"]
RuntimeSkillStatus = Literal["published", "disabled", "revoked"]
SkillSource = Literal["builtin", "tenant", "imported"]


# ───────────────────────────────────────────── 技能 · 运行时视角


class SkillFile(BaseModel):
    path: str
    size: int
    sha256: str
    executable: bool = False


class SkillVersionOut(BaseModel):
    """一个已发布（含已停用 / 已下架）的技能版本。

    ★ 除 status / status_reason 外全部不可变：运行时据此永久缓存。
    """

    slug: str
    version: int
    status: RuntimeSkillStatus
    description: str
    content_hash: str
    size_bytes: int
    file_count: int
    has_scripts: bool
    files: list[SkillFile] = Field(default_factory=list)
    source: SkillSource = "builtin"
    scan_result: dict[str, Any] = Field(default_factory=dict)
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    published_by: str | None = None
    published_at: datetime | None = None
    status_reason: str | None = None


class SkillVersionBrief(BaseModel):
    version: int
    status: RuntimeSkillStatus


class SkillCatalogItem(BaseModel):
    """技能目录里的一项（编辑器选择器）。latest 只看 published。"""

    slug: str
    source: SkillSource
    latest: int | None = None
    description: str | None = None
    has_scripts: bool | None = None
    versions: list[SkillVersionBrief] = Field(default_factory=list)


class RevokedSkill(BaseModel):
    slug: str
    version: int
    reason: str | None = None
    at: datetime | None = None


# ───────────────────────────────────────────── 技能 · 管理视角


class SkillVersionAdminOut(BaseModel):
    """管理页看到的版本：含审查中 / 被拒的上传（运行时拿不到这些）。"""

    id: UUID
    slug: str
    version: int | None
    status: SkillStatus
    description: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    size_bytes: int
    file_count: int
    has_scripts: bool
    files: list[SkillFile] = Field(default_factory=list)
    scan_result: dict[str, Any] = Field(default_factory=dict)
    uploaded_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    status_reason: str | None = None
    published_by: str | None = None
    published_at: datetime | None = None
    created_at: datetime


class SkillDetailOut(BaseModel):
    slug: str
    source: SkillSource
    origin_url: str | None = None
    created_by: str
    created_at: datetime
    latest: int | None = None
    versions: list[SkillVersionAdminOut] = Field(default_factory=list)


class ReviewDecisionIn(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


class StatusChangeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=4000)


# ───────────────────────────────────────────── MCP


def _check_headers(headers: dict[str, str]) -> dict[str, str]:
    for name, value in headers.items():
        if SENSITIVE_HEADER.match(name) and not _ENV_REF.search(value):
            msg = (
                f"header {name!r} 看起来是凭据，值必须写成 ${{ENV}} 占位符 —— "
                "配置库不保存真凭据，解析在运行时进程里做"
            )
            raise ValueError(msg)
    return headers


class McpServerIn(BaseModel):
    name: str = Field(pattern=MCP_SERVER_NAME_PATTERN)
    display_name: str = ""
    description: str = ""
    transport: Literal["streamable_http", "sse"] = "streamable_http"
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    #: user 作用域要等 OAuth 上线（P3）；现在只接受 platform
    credential_scope: Literal["platform"] = "platform"
    call_timeout_s: float | None = Field(default=None, gt=0)
    review_required: bool = True
    status: Literal["enabled", "disabled"] = "enabled"

    _headers = field_validator("headers")(_check_headers)


class McpServerPatch(BaseModel):
    """name 不可改：它是工具前缀，改名 = 另一个 server。"""

    display_name: str | None = None
    description: str | None = None
    transport: Literal["streamable_http", "sse"] | None = None
    url: str | None = Field(default=None, min_length=1)
    headers: dict[str, str] | None = None
    call_timeout_s: float | None = Field(default=None, gt=0)
    review_required: bool | None = None
    status: Literal["enabled", "disabled"] | None = None

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return None if value is None else _check_headers(value)


class McpServerOut(BaseModel):
    """与运行时 McpServerConfig 同构的字段（name/url/transport/headers/enabled/
    call_timeout_s），外加注册表自己的元数据。headers 里只有占位符。"""

    name: str
    display_name: str = ""
    description: str = ""
    transport: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    credential_scope: str = "platform"
    call_timeout_s: float | None = None
    review_required: bool = True
    enabled: bool = True
    updated_by: str | None = None
    updated_at: datetime | None = None


class ToolReviewIn(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    #: 来自运行时的 MCP 详情接口 —— 配置服务不连 server，无法也不需要验证它
    digest: str = Field(min_length=8, max_length=80)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=4000)


class ToolReviewOut(BaseModel):
    server: str
    tool_name: str
    digest: str
    decision: Literal["approved", "rejected"]
    decided_by: str
    decided_at: datetime
    note: str = ""


class AuditOut(BaseModel):
    id: UUID
    at: datetime
    actor: str
    action: str
    target_kind: str
    target_id: str
    detail: dict[str, Any] = Field(default_factory=dict)
