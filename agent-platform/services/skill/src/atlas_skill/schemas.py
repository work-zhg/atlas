"""技能应用的对外契约。

★ 这是 atlas_agent **唯一**允许 import 的模块（import-linter）。所以它只依赖
  pydantic：import 它不该把 sqlalchemy / boto3 / fastapi 带进调用方。
★ 运行时用的模型（SkillVersionOut 等）字段只增不改；新增字段必须有默认值 ——
  运行时把它们永久缓存在磁盘上，旧缓存要能被新代码读回来。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field

__all__ = [
    "SKILL_SLUG_PATTERN",
    "AuditOut",
    "MarketInstalled",
    "MarketSkillOut",
    "MarketVersionOut",
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
]

SKILL_SLUG_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"

SkillStatus = Literal["pending_review", "rejected", "published", "disabled", "revoked"]
RuntimeSkillStatus = Literal["published", "disabled", "revoked"]
#: market = 从技能市场安装的（产品自带的技能，版本号与市场一致）
SkillSource = Literal["builtin", "tenant", "imported", "market"]


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
    #: 对象存储里的目录（运行时从这里拷进使用者的技能目录）。多租户以后第一层是租户，
    #: 运行时不再自己拼路径。旧缓存里没有它：为空时按多租户之前的 _skills/{slug}/{version}/
    storage_key: str | None = None


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

    #: 上传 id（库里版本的 uuid 逻辑主键）
    id: UUID = Field(validation_alias=AliasChoices("uuid", "id"))
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


class AuditOut(BaseModel):
    id: UUID = Field(validation_alias=AliasChoices("uuid", "id"))
    at: datetime = Field(validation_alias=AliasChoices("created_at", "at"))
    actor: str
    action: str
    target_kind: str
    target_id: str
    detail: dict[str, Any] = Field(default_factory=dict)


# ───────────────────────────────────────────── 技能市场


class MarketVersionOut(BaseModel):
    """技能市场里的一个版本。"""

    slug: str
    version: int
    status: Literal["published", "revoked"]
    description: str
    content_hash: str
    size_bytes: int
    file_count: int
    has_scripts: bool
    files: list[SkillFile] = Field(default_factory=list)
    scan_result: dict[str, Any] = Field(default_factory=dict)
    status_reason: str | None = None
    published_by: str
    published_at: datetime


class MarketSkillOut(BaseModel):
    slug: str
    install_by_default: bool
    latest: int | None = None
    description: str | None = None
    versions: list[MarketVersionOut] = Field(default_factory=list)
    #: 本租户已经装了哪些版本（租户视角才有）
    installed: list[int] = Field(default_factory=list)


class MarketInstalled(BaseModel):
    slug: str
    version: int
    #: 这次拷进来的；False = 本租户早就装过这个版本
    copied: bool
