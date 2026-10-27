"""用户中心的表（各模块详细设计的「数据模型」一节）。

主键约定：`id` 自增主键只在库内使用，不对外、不做关联；`uuid` 逻辑主键（UUIDv7）
用于全部关联、外键、API。关联字段命名 `xxx_uuid`，外键引用对方表的 `uuid`。

租户（租户设计 §04 · §05）：
  · 租户表继承 TenantOwned（tenant_uuid 必填），查询自动按当前租户过滤（db/tenancy.py）
  · 租户表之间的引用用**复合外键** (tenant_uuid, xxx_uuid) → 对方 (tenant_uuid, uuid)：
    跨租户的引用在数据库层面写不进去
  · 应用、操作、菜单、数据编码属于平台；角色两级（tenant_uuid 空 = 应用内置，所有租户共用）
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
    ForeignKeyConstraint,
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
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

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


# ───────────────────────────────────────────── 租户


class Tenant(Keys, Audited, Base):
    """租户：一个相互隔离的组织空间（租户设计 §02）。平台租户（kind=platform）全局唯一。"""

    __tablename__ = "uc_tenant"

    #: 企业代码：登录时用；小写字母开头，字母、数字与 -；创建后不可改
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="customer", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: 人员配额（未停用的人；数字员工不计）。空 = 不限
    max_users: Mapped[int | None] = mapped_column(Integer)
    #: 到期后视同 suspended
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("kind IN ('platform','customer')", name="kind_enum"),
        CheckConstraint("status IN ('active','suspended','closed')", name="status_enum"),
        Index(
            "uq_uc_tenant_platform", "kind", unique=True, postgresql_where=text("kind = 'platform'")
        ),
    )


class TenantOwned:
    """租户数据：tenant_uuid 必填；查询自动按当前租户过滤，新行自动填当前租户（db/tenancy.py）。"""

    @declared_attr
    def tenant_uuid(cls) -> Mapped[UUID]:
        return mapped_column(ForeignKey("uc_tenant.uuid"), nullable=False)


# ───────────────────────────────────────────── 组织 / 用户


class Dept(Keys, Audited, TenantOwned, Base):
    __tablename__ = "uc_dept"

    parent_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    #: ★ 与 uc_user.dept_uuid 互相引用：外键用 use_alter 在两表建好后再补
    leader_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 物化路径 '/{root}/{rd}/{trade}/'，用于子树查询（组织设计 §15）
    path: Mapped[str] = mapped_column(String(400), nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("parent_uuid", "name", name="uq_uc_dept_sibling_name"),
        CheckConstraint("position('/' in name) = 0", name="name_no_slash"),
        CheckConstraint("depth BETWEEN 1 AND 10", name="depth_range"),
        UniqueConstraint("tenant_uuid", "uuid", name="uq_uc_dept_tenant_uuid"),
        # 每个租户一棵树、一个根
        Index(
            "uq_uc_dept_single_root",
            "tenant_uuid",
            unique=True,
            postgresql_where=text("parent_uuid IS NULL"),
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "parent_uuid"],
            ["uc_dept.tenant_uuid", "uc_dept.uuid"],
            name="fk_uc_dept_parent",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "leader_uuid"],
            ["uc_user.tenant_uuid", "uc_user.uuid"],
            name="fk_uc_dept_leader_uuid",
            use_alter=True,
        ),
        Index("ix_uc_dept_path", "path", postgresql_ops={"path": "varchar_pattern_ops"}),
        Index("ix_uc_dept_parent_sort", "parent_uuid", "sort"),
    )


class User(Keys, Audited, TenantOwned, Base):
    """用户：人（human），或数字员工（digital，数字员工设计 §3）。

    数字员工由接入应用经开放接口创建（一个 Agent 一个），和人一样可以被授角色、岗位、数据权限，
    但没有密码（不能登录）、没有部门（不进组织树，部门授权落不到它身上）、不占人员配额。
    """

    __tablename__ = "uc_user"

    #: 账号、邮箱在**租户内**唯一（不同租户可以同名）
    account: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 人必填；数字员工没有
    email: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(20))
    #: 人必填；数字员工必须为空
    dept_uuid: Mapped[UUID | None] = mapped_column(UUIDType, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: human | digital
    user_type: Mapped[str] = mapped_column(
        String(8), default="human", server_default="human", nullable=False
    )
    #: 数字员工的负责人（必须是人）。出了问题找谁；不把数字员工挂到负责人的部门
    owner_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    #: 创建数字员工的应用：只有它能改、停用、启用
    source_app_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_app.uuid"))
    #: 来源应用里的对象 id（Atlas = agent id）：(租户, 来源应用, external_ref) 唯一，幂等创建的依据
    external_ref: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(200))
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    disabled_by: Mapped[UUID | None] = mapped_column(UUIDType)
    disabled_reason: Mapped[str | None] = mapped_column(String(200))
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="status_enum"),
        CheckConstraint("user_type IN ('human','digital')", name="user_type_enum"),
        CheckConstraint("user_type = 'digital' OR email IS NOT NULL", name="human_email"),
        CheckConstraint("(user_type = 'digital') = (dept_uuid IS NULL)", name="digital_no_dept"),
        CheckConstraint(
            "(user_type = 'digital') = (owner_uuid IS NOT NULL AND source_app_uuid IS NOT NULL)",
            name="digital_owner_app",
        ),
        UniqueConstraint("tenant_uuid", "account", name="uq_uc_user_account"),
        UniqueConstraint("tenant_uuid", "email", name="uq_uc_user_email"),
        UniqueConstraint("tenant_uuid", "uuid", name="uq_uc_user_tenant_uuid"),
        ForeignKeyConstraint(
            ["tenant_uuid", "dept_uuid"],
            ["uc_dept.tenant_uuid", "uc_dept.uuid"],
            name="fk_uc_user_dept",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "owner_uuid"],
            ["uc_user.tenant_uuid", "uc_user.uuid"],
            name="fk_uc_user_owner",
        ),
        Index(
            "uq_uc_user_digital_ref",
            "tenant_uuid",
            "source_app_uuid",
            "external_ref",
            unique=True,
            postgresql_where=text("user_type = 'digital'"),
        ),
        Index("ix_uc_user_type_status", "user_type", "status"),
    )

    @property
    def is_digital(self) -> bool:
        return self.user_type == "digital"


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

    #: 显式填写、不参与自动过滤：企业代码不存在的失败登录没有租户
    tenant_uuid: Mapped[UUID | None] = mapped_column(UUIDType, index=True)
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
    #: 应用编码（内网身份传递 §3）：内网调用开放接口时请求头 X-Caller-App 写它，如 atlas、teamflow。
    #: 空 = 不接受内网请求头调用（只能用 App Key / Secret）
    code: Mapped[str | None] = mapped_column(String(32), unique=True)
    icon: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(String(200))
    #: 内置应用为空
    app_key: Mapped[str | None] = mapped_column(String(40), unique=True)
    secret_hash: Mapped[str | None] = mapped_column(String(64))
    secret_tail: Mapped[str | None] = mapped_column(String(4))
    secret_rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: P1：Webhook
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="status_enum"),)


class TenantApp(Keys, TenantOwned, Base):
    """租户开通应用（租户设计 §05）：开通状态与本租户的可访问范围（全员或指定部门）。

    内置应用（用户中心）不需要这一行：每个租户都可以用。
    """

    __tablename__ = "uc_tenant_app"

    app_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_app.uuid", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: 全员可访问；否则看 uc_app_scope_dept。★ 默认 False：开通后先配范围，避免对全员开放
    scope_all: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled_by: Mapped[UUID | None] = mapped_column(UUIDType)
    enabled_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="status_enum"),
        UniqueConstraint("tenant_uuid", "app_uuid", name="uq_uc_tenant_app"),
    )


class AppScopeDept(Keys, TenantOwned, Base):
    """某租户开通某应用后的可访问部门（含下级）。"""

    __tablename__ = "uc_app_scope_dept"

    app_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_app.uuid", ondelete="CASCADE"), nullable=False
    )
    dept_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)

    __table_args__ = (
        UniqueConstraint("app_uuid", "dept_uuid", name="uq_uc_app_scope"),
        ForeignKeyConstraint(
            ["tenant_uuid", "dept_uuid"],
            ["uc_dept.tenant_uuid", "uc_dept.uuid"],
            name="fk_uc_app_scope_dept_dept",
            ondelete="CASCADE",
        ),
    )


class AppToken(Keys, Base):
    """开放接口的接口令牌（2 小时）。★ 同 Session：首版放 PG。"""

    __tablename__ = "uc_app_token"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    app_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_app.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 令牌绑定 (应用, 租户)：开放接口的每个请求都限定在这个租户内（租户设计 §09）。
    #: ★ 不继承 TenantOwned：解析令牌时还不知道租户，这一列显式读写
    tenant_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_tenant.uuid", ondelete="CASCADE"), nullable=False
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
    """角色两级（租户设计 §04）：tenant_uuid 空 = 应用内置，所有租户可授、只有平台能改；
    非空 = 该租户自定义。查询时只看得到「内置 + 本租户」的（db/tenancy.py）。"""

    __tablename__ = "uc_role"

    app_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_app.uuid"), nullable=False)
    tenant_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_tenant.uuid"))
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index(
            "uq_uc_role_code",
            text("coalesce(tenant_uuid, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "code",
            unique=True,
        ),
        Index(
            "uq_uc_role_name",
            text("coalesce(tenant_uuid, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "app_uuid",
            "name",
            unique=True,
        ),
    )


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


# ───────────────────────────────────────────── 权限


class Position(Keys, Audited, TenantOwned, Base):
    __tablename__ = "uc_position"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_uuid", "code", name="uq_uc_position_code"),
        UniqueConstraint("tenant_uuid", "name", name="uq_uc_position_name"),
        UniqueConstraint("tenant_uuid", "uuid", name="uq_uc_position_tenant_uuid"),
    )


class PositionRole(Keys, TenantOwned, Base):
    __tablename__ = "uc_position_role"

    position_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    role_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_role.uuid", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("position_uuid", "role_uuid", name="uq_uc_position_role"),
        ForeignKeyConstraint(
            ["tenant_uuid", "position_uuid"],
            ["uc_position.tenant_uuid", "uc_position.uuid"],
            name="fk_uc_position_role_position",
            ondelete="CASCADE",
        ),
    )


class Grant(Keys, TenantOwned, Base):
    """角色授权：岗位 或 角色 → 用户 或 部门（权限设计 §13）。

    ★ 主体与岗位用复合外键锁在同一租户；角色可以是应用内置（无租户）的，由服务层校验。
    """

    __tablename__ = "uc_grant"

    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    position_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    role_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("uc_role.uuid", ondelete="CASCADE"))
    subject_type: Mapped[str] = mapped_column(String(8), nullable=False)
    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    dept_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
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
        ForeignKeyConstraint(
            ["tenant_uuid", "position_uuid"],
            ["uc_position.tenant_uuid", "uc_position.uuid"],
            name="fk_uc_grant_position",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "user_uuid"],
            ["uc_user.tenant_uuid", "uc_user.uuid"],
            name="fk_uc_grant_user",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "dept_uuid"],
            ["uc_dept.tenant_uuid", "uc_dept.uuid"],
            name="fk_uc_grant_dept",
            ondelete="CASCADE",
        ),
    )


class DataObject(Keys, TenantOwned, Base):
    """数据对象：(租户, 数据编码, data_id) 唯一 —— 两个租户的同一个 data_id 互不相干。"""

    __tablename__ = "uc_data_object"

    data_type_uuid: Mapped[UUID] = mapped_column(ForeignKey("uc_data_type.uuid"), nullable=False)
    data_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_uuid", "data_type_uuid", "data_id", name="uq_uc_data_object"),
        UniqueConstraint("tenant_uuid", "uuid", name="uq_uc_data_object_tenant_uuid"),
    )


class DataAcl(Keys, TenantOwned, Base):
    __tablename__ = "uc_data_acl"

    data_object_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: 1 只读 | 2 读写 | 3 Owner
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(8), nullable=False)
    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    dept_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
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
        ForeignKeyConstraint(
            ["tenant_uuid", "data_object_uuid"],
            ["uc_data_object.tenant_uuid", "uc_data_object.uuid"],
            name="fk_uc_data_acl_object",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "user_uuid"],
            ["uc_user.tenant_uuid", "uc_user.uuid"],
            name="fk_uc_data_acl_user",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "dept_uuid"],
            ["uc_dept.tenant_uuid", "uc_dept.uuid"],
            name="fk_uc_data_acl_dept",
            ondelete="CASCADE",
        ),
    )


class DataTypeAcl(Keys, TenantOwned, Base):
    """数据编码级授权：对该编码下**全部**数据的级别（如「全部智能体 · Owner」）。

    ★ 属于数据权限领域，不经角色 / 操作码表达（替代原 uc_data_type.admin_operation_code）。
      对某条数据的有效级别 = max(该数据的授权, 该编码的授权)。
    ★ 不计入某条数据的「至少保留一名 Owner」，也不出现在数据的持有人列表里 ——
      它是对全部数据的兜底管理，不是某条数据的成员。
    """

    __tablename__ = "uc_data_type_acl"

    data_type_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("uc_data_type.uuid", ondelete="CASCADE"), nullable=False
    )
    #: 1 只读 | 2 读写 | 3 Owner
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(8), nullable=False)
    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    dept_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    include_sub: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(UUIDType)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_uuid", "user_uuid"],
            ["uc_user.tenant_uuid", "uc_user.uuid"],
            name="fk_uc_data_type_acl_user",
        ),
        ForeignKeyConstraint(
            ["tenant_uuid", "dept_uuid"],
            ["uc_dept.tenant_uuid", "uc_dept.uuid"],
            name="fk_uc_data_type_acl_dept",
            ondelete="CASCADE",
        ),
        CheckConstraint("level IN (1, 2, 3)", name="level_enum"),
        CheckConstraint(
            "((subject_type = 'user') = (user_uuid IS NOT NULL))"
            " AND ((subject_type = 'dept') = (dept_uuid IS NOT NULL))",
            name="subject",
        ),
        CheckConstraint("subject_type = 'dept' OR NOT include_sub", name="sub_only_dept"),
        Index(
            "uq_uc_data_type_acl",
            "tenant_uuid",
            "data_type_uuid",
            text("coalesce(user_uuid, dept_uuid)"),
            unique=True,
        ),
        Index(
            "ix_uc_data_type_acl_user", "user_uuid", postgresql_where=text("user_uuid IS NOT NULL")
        ),
        Index(
            "ix_uc_data_type_acl_dept", "dept_uuid", postgresql_where=text("dept_uuid IS NOT NULL")
        ),
    )


# ───────────────────────────────────────────── 审计


class AuditEvent(Keys, Base):
    """只追加。查询页面属于后续的审计日志模块（总体设计 §11）。"""

    __tablename__ = "uc_audit_event"

    #: 发生在哪个租户；平台级操作（建租户、维护应用目录）为空。写入时按当前租户自动填
    tenant_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
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
        Index("ix_uc_audit_event_tenant", "tenant_uuid", text("occurred_at DESC")),
        Index("ix_uc_audit_event_target", "target_type", "target_uuid"),
    )
