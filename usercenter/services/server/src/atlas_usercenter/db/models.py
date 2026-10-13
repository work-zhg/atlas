"""用户中心的表（各模块详细设计的「数据模型」一节）。

主键约定：`id` 自增主键只在库内使用，不对外、不做关联；`uuid` 逻辑主键（UUIDv7）
用于全部关联、外键、API。关联字段命名 `xxx_uuid`，外键引用对方表的 `uuid`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..ids import uuid7
from .types import JSONType, UTCDateTime, UUIDType, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Keys:
    """自增主键 + 逻辑主键。"""

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False, default=uuid7)


class Audited:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    updated_by: Mapped[UUID | None] = mapped_column(UUIDType)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# ───────────────────────────────────────────── 组织 / 用户


class Dept(Keys, Audited, Base):
    __tablename__ = "uc_dept"

    parent_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_dept.uuid"))
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    #: ★ 与 uc_user.dept_uuid 互相引用：外键用 use_alter 在两表建好后再补
    leader_uuid: Mapped[UUID | None] = mapped_column(
        ForeignKey("uc_user.uuid", use_alter=True, name="fk_uc_dept_leader_uuid")
    )
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 物化路径 '/{root}/{rd}/{trade}/'，用于子树查询（组织设计 §15）
    path: Mapped[str] = mapped_column(String(400), nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("parent_uuid", "name", name="uq_uc_dept_sibling_name"),
        CheckConstraint("position('/' in name) = 0", name="name_no_slash"),
        CheckConstraint("depth BETWEEN 1 AND 10", name="depth_range"),
        Index(
            "uq_uc_dept_single_root",
            text("(parent_uuid IS NULL)"),
            unique=True,
            postgresql_where=text("parent_uuid IS NULL"),
        ),
        Index("ix_uc_dept_path", "path", postgresql_ops={"path": "varchar_pattern_ops"}),
        Index("ix_uc_dept_parent_sort", "parent_uuid", "sort"),
    )


class User(Keys, Audited, Base):
    __tablename__ = "uc_user"

    account: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    dept_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_dept.uuid"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disabled_by: Mapped[UUID | None] = mapped_column(UUIDType)
    disabled_reason: Mapped[str | None] = mapped_column(String(200))
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="status_enum"),)


class Credential(Keys, Base):
    __tablename__ = "uc_user_credential"

    user_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_user.uuid"), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    temp_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Session(Keys, Base):
    """管理台登录会话。★ 设计里放 Redis；首版放 PG，少一个依赖（见 README 偏差说明）。"""

    __tablename__ = "uc_session"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_user.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class LoginLog(Keys, Base):
    __tablename__ = "uc_login_log"

    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType, index=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(45))
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    #: 经开放接口校验密码时为调用方应用名；用户中心管理台登录为空
    app_name: Mapped[str | None] = mapped_column(String(50))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# ───────────────────────────────────────────── 应用接入


class App(Keys, Audited, Base):
    __tablename__ = "uc_app"

    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    icon: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(String(200))
    #: 内置应用为空
    app_key: Mapped[str | None] = mapped_column(String(40), unique=True)
    secret_hash: Mapped[str | None] = mapped_column(String(64))
    secret_tail: Mapped[str | None] = mapped_column(String(4))
    secret_rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scope_all: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: P1：Webhook
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="status_enum"),)


class AppScopeDept(Keys, Base):
    __tablename__ = "uc_app_scope_dept"

    app_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_app.uuid", ondelete="CASCADE"), nullable=False
    )
    dept_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_dept.uuid", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (UniqueConstraint("app_uuid", "dept_uuid", name="uq_uc_app_scope"),)


class AppToken(Keys, Base):
    """开放接口的接口令牌（2 小时）。★ 同 Session：首版放 PG。"""

    __tablename__ = "uc_app_token"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    app_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_app.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Operation(Keys, Base):
    __tablename__ = "uc_operation"

    app_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_app.uuid"), nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)

    __table_args__ = (UniqueConstraint("app_uuid", "code", name="uq_uc_operation_code"),)


class Menu(Keys, Base):
    __tablename__ = "uc_menu"

    app_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_app.uuid"), nullable=False)
    parent_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_menu.uuid"))
    type: Mapped[str] = mapped_column(String(8), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(16))
    path: Mapped[str | None] = mapped_column(String(200))
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)

    __table_args__ = (
        UniqueConstraint("app_uuid", "code", name="uq_uc_menu_code"),
        CheckConstraint("type IN ('dir','menu')", name="type_enum"),
        CheckConstraint("(type = 'menu') = (path IS NOT NULL)", name="path_iff_menu"),
        CheckConstraint("type = 'menu' OR NOT is_public", name="public_only_menu"),
        Index(
            "uq_uc_menu_path",
            "app_uuid",
            "path",
            unique=True,
            postgresql_where=text("path IS NOT NULL"),
        ),
    )


class Role(Keys, Audited, Base):
    __tablename__ = "uc_role"

    app_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_app.uuid"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (UniqueConstraint("app_uuid", "name", name="uq_uc_role_name"),)


class RoleOperation(Keys, Base):
    __tablename__ = "uc_role_operation"

    role_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_role.uuid", ondelete="CASCADE"), nullable=False
    )
    operation_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_operation.uuid", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (UniqueConstraint("role_uuid", "operation_uuid", name="uq_uc_role_op"),)


class RoleMenu(Keys, Base):
    __tablename__ = "uc_role_menu"

    role_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_role.uuid", ondelete="CASCADE"), nullable=False
    )
    menu_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_menu.uuid", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (UniqueConstraint("role_uuid", "menu_uuid", name="uq_uc_role_menu"),)


class DataType(Keys, Base):
    __tablename__ = "uc_data_type"

    app_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_app.uuid"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    #: 数据管理员操作码：拥有它（在本应用的有效操作中）的用户可修改该编码下任一数据的授权
    #: （如 TeamFlow 的 team:manage_all），用于 Owner 全部离职时由平台管理员接手
    admin_operation_code: Mapped[str | None] = mapped_column(String(64))


# ───────────────────────────────────────────── 权限


class Position(Keys, Audited, Base):
    __tablename__ = "uc_position"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PositionRole(Keys, Base):
    __tablename__ = "uc_position_role"

    position_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_position.uuid", ondelete="CASCADE"), nullable=False
    )
    role_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_role.uuid", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (UniqueConstraint("position_uuid", "role_uuid", name="uq_uc_position_role"),)


class Grant(Keys, Base):
    """角色授权：岗位 或 角色 → 用户 或 部门（权限设计 §13）。"""

    __tablename__ = "uc_grant"

    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    position_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_position.uuid"))
    role_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_role.uuid", ondelete="CASCADE"))
    subject_type: Mapped[str] = mapped_column(String(8), nullable=False)
    user_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_user.uuid"))
    dept_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_dept.uuid", ondelete="CASCADE"))
    include_sub: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(UUIDType)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "((kind = 'position') = (position_uuid IS NOT NULL))"
            " AND ((kind = 'role') = (role_uuid IS NOT NULL))",
            name="kind_target",
        ),
        CheckConstraint(
            "((subject_type = 'user') = (user_uuid IS NOT NULL))"
            " AND ((subject_type = 'dept') = (dept_uuid IS NOT NULL))",
            name="subject",
        ),
        CheckConstraint("subject_type = 'dept' OR NOT include_sub", name="sub_only_dept"),
        Index(
            "uq_uc_grant",
            "kind",
            text("coalesce(position_uuid, role_uuid)"),
            text("coalesce(user_uuid, dept_uuid)"),
            unique=True,
        ),
        Index("ix_uc_grant_user", "user_uuid", postgresql_where=text("user_uuid IS NOT NULL")),
        Index("ix_uc_grant_dept", "dept_uuid", postgresql_where=text("dept_uuid IS NOT NULL")),
        Index("ix_uc_grant_role", "role_uuid", postgresql_where=text("role_uuid IS NOT NULL")),
        Index(
            "ix_uc_grant_pos", "position_uuid", postgresql_where=text("position_uuid IS NOT NULL")
        ),
    )


class DataObject(Keys, Base):
    __tablename__ = "uc_data_object"

    data_type_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_data_type.uuid"), nullable=False)
    data_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("data_type_uuid", "data_id", name="uq_uc_data_object"),)


class DataAcl(Keys, Base):
    __tablename__ = "uc_data_acl"

    data_object_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_data_object.uuid", ondelete="CASCADE"), nullable=False
    )
    #: 1 只读 | 2 读写 | 3 Owner
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(8), nullable=False)
    user_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_user.uuid"))
    dept_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_dept.uuid", ondelete="CASCADE"))
    include_sub: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(UUIDType)
    granted_app_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("level IN (1, 2, 3)", name="level_enum"),
        CheckConstraint(
            "((subject_type = 'user') = (user_uuid IS NOT NULL))"
            " AND ((subject_type = 'dept') = (dept_uuid IS NOT NULL))",
            name="subject",
        ),
        CheckConstraint("subject_type = 'dept' OR NOT include_sub", name="sub_only_dept"),
        Index(
            "uq_uc_data_acl",
            "data_object_uuid",
            text("coalesce(user_uuid, dept_uuid)"),
            unique=True,
        ),
        Index("ix_uc_data_acl_user", "user_uuid", postgresql_where=text("user_uuid IS NOT NULL")),
        Index("ix_uc_data_acl_dept", "dept_uuid", postgresql_where=text("dept_uuid IS NOT NULL")),
    )


# ───────────────────────────────────────────── 审计


class AuditEvent(Keys, Base):
    """只追加。查询页面属于后续的审计日志模块（总体设计 §11）。"""

    __tablename__ = "uc_audit_event"

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(8), nullable=False)
    actor_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    target_name: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    __table_args__ = (
        Index("ix_uc_audit_event_time", text("occurred_at DESC")),
        Index("ix_uc_audit_event_target", "target_type", "target_uuid"),
    )
