"""配置库的表（设计 §5.1、§8）。

★ 不建外键（与运行时 0013 起的约定一致）：引用完整性由 service 层保证。
★ 技能版本行发布后，`description` / `content_hash` / `files` / `storage_key`
  **永不修改**，只有 status 一族字段可变 —— 运行时按版本永久缓存的依据就是它。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

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

from .types import JSONType, UTCDateTime, UUIDType, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

#: 技能版本的状态。
#:
#: pending_review —— 已上传、机械扫描通过，等人工审查（含脚本 / 非 builtin 必经）
#: rejected       —— 审查不通过；草稿对象已删，行保留作记录
#: published      —— 已发布，可被新引用
#: disabled       —— 停用：不可被**新**引用，已引用的照常（可重新启用）
#: revoked        —— 紧急下架：所有引用在装配时跳过（不可撤销）
#:
#: ★ 没有 draft / scanning：扫描是同步做完的（≤ 20MB 的正则扫描，毫秒级），
#:   多一个中间态只会多一种「进程重启后卡在半路」的情况。
SKILL_STATUSES = ("pending_review", "rejected", "published", "disabled", "revoked")
#: 运行时看得见的状态。草稿态（pending_review / rejected）结构性地拿不到。
RUNTIME_VISIBLE = ("published", "disabled", "revoked")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Skill(Base):
    __tablename__ = "skill"

    slug: Mapped[str] = mapped_column(String(63), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    #: imported 必填：第三方出处
    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("source IN ('builtin','tenant','imported')", name="source_enum"),
    )


class SkillVersion(Base):
    __tablename__ = "skill_version"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    #: ★ 发布时才分配。审查不通过的上传不占版本号 —— 否则版本清单里会出现
    #:   「v4 不存在」的空洞，而使用者无从知道那是被拒了还是丢了。
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    description: Mapped[str] = mapped_column(Text, nullable=False)
    frontmatter: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: 草稿：_skills_drafts/{id}/；发布后：_skills/{slug}/{version}/
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    has_scripts: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: [{path, size, sha256, executable}]
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    scan_result: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    uploaded_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_skill_version_slug_version"),
        Index("ix_skill_version_slug_status", "slug", "status"),
        CheckConstraint(
            "status IN ('pending_review','rejected','published','disabled','revoked')",
            name="status_enum",
        ),
        CheckConstraint(
            "version IS NOT NULL OR status IN ('pending_review','rejected')",
            name="released_has_version",
        ),
    )


class McpServer(Base):
    """MCP server 定义（设计 §8.1）。

    ★ headers 的值只存 `${ENV}` 占位符（或不含凭据的固定值）。配置服务**永远
      看不到真凭据** —— 解析发生在运行时进程里。
    """

    __tablename__ = "mcp_server"

    name: Mapped[str] = mapped_column(String(24), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    transport: Mapped[str] = mapped_column(String(24), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    headers: Mapped[dict[str, str]] = mapped_column(JSONType, nullable=False, default=dict)
    credential_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    call_timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("status IN ('enabled','disabled')", name="status_enum"),
        CheckConstraint("credential_scope IN ('platform','user')", name="scope_enum"),
        CheckConstraint("transport IN ('streamable_http','sse')", name="transport_enum"),
    )


class McpToolReview(Base):
    """工具定义复核结论（设计 §8.3）。按 digest 记 —— 定义一变就是另一行。"""

    __tablename__ = "mcp_tool_review"

    server: Mapped[str] = mapped_column(String(24), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(80), primary_key=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (CheckConstraint("decision IN ('approved','rejected')", name="decision_enum"),)


class ConfigAudit(Base):
    """写操作的审计。与业务写入同一事务 —— 不会出现「改了但没记」。"""

    __tablename__ = "config_audit"

    id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True, default=uuid4)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    __table_args__ = (Index("ix_config_audit_target", "target_kind", "target_id", "at"),)
