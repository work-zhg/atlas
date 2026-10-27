"""记忆库的表（doc/memory-design.html §3）。

    mem_scope     作用域（管理员配置）
    mem_instance  作用域实例：「作用域 + 关联主键」唯一；带实例级计数器（版本号、条目编号）
    mem_topic     主题
    mem_entry     条目（含已删除的：删除只改状态，彻底删除才真删）
    mem_revision  修订历史：每次写入删除了哪些、新增了哪一条；撤销、拆分、移动也各记一条

★ 自增 id + uuid 逻辑主键；关联与 API 一律用 uuid（字段名 xxx_uuid）。
★ 不建外键（与其它服务一致）：引用完整性由 service 层保证。
★ 条目编号、版本号都由实例行上的计数器分配，只在实例锁内递增（store.py）：
    · 编号在实例内递增，之后不再改变 —— 移动、拆分都不动它（§3.4）
    · 版本号按实例统一递增：主题每次变化都拿一个新值。不用主题各自从 1 数起 ——
      主题被拆掉后再建同名主题，各自计数会让新主题的版本号撞上客户端记着的旧值
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .types import JSONType, UTCDateTime, UUIDType, utcnow, uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}

#: 主题状态。active = 正常；split = 已被拆分（split_into 记着拆成了哪些）；deleted = 删除的空主题。
#: ★ 不是 active 的行保留：写入的目标主题已被拆掉时，要能告诉客户端「拆成了哪些」（§3.6）
TOPIC_STATUSES = ("active", "split", "deleted")
#: 条目状态。只有 active 的会被列出和加载；deleted 的留在修订历史里，可以撤销、恢复（图 2）
ENTRY_STATUSES = ("active", "deleted")
#: 修订种类
REVISION_KINDS = ("write", "undo", "restore", "move", "split")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _Row:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, default=uuid7, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Scope(_Row, Base):
    __tablename__ = "mem_scope"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 关联主键的字段名，有序，例如 ["uid"]、["uid", "project"]。★ 建好后不能改：
    #: 改了已有实例就对不上了
    key_fields: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    #: 是否按用户中心的数据权限校验调用用户（§3.7）
    check_permission: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 对应的用户中心数据编码（挂在本服务的应用名下）。开关打开时必填
    data_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class Instance(_Row, Base):
    __tablename__ = "mem_instance"

    scope_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: 关联主键：{字段名: 值}。按其中一部分列实例用 JSONB 包含查询（删除某个用户的全部数据）
    key: Mapped[dict[str, str]] = mapped_column(JSONType, nullable=False)
    #: 规范形式 "uid=…,project=…"（按作用域的字段顺序）—— 唯一约束与展示用
    key_text: Mapped[str] = mapped_column(String(512), nullable=False)
    #: 版本号计数器（见模块说明）
    version_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: 条目编号计数器
    entry_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: 已在用户中心登记归属（只对打开数据权限开关的作用域有意义）。
    #: ★ 用户中心里的数据标识是本行的 uuid（36 字符）：关联主键拼起来可能超过用户中心的 64 字符
    uc_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 第一次登记时给出的 Owner（追溯用；权限以用户中心为准）
    owner_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("scope_uuid", "key_text"),
        Index("ix_mem_instance_key", "key", postgresql_using="gin"),
    )


class Topic(_Row, Base):
    __tablename__ = "mem_topic"

    instance_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 扩展属性：客户端定义，服务端原样保存、不解释（Atlas 用它标记「常驻」）
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: 当前大小：有效条目数与字符数。服务端只报告，不据此拒绝写入（§3.3）
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 拆成了哪些主题（status=split 时）
    split_into: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active','split','deleted')", name="status"),
        # 编码只在**有效**主题之间唯一：拆掉之后可以再建同名主题
        Index(
            "ux_mem_topic_code",
            "instance_uuid",
            "code",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class Entry(_Row, Base):
    """一条条目。created_at = 入库时间（追溯用）；occurred_at = 客户端给的时间（判断先后用）。"""

    __tablename__ = "mem_entry"

    instance_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    topic_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: 客户端给的标签，原样保存（Atlas：user / agent / backfill）
    source: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    #: 出处：客户端给的说明，原样保存（Atlas：{"thread": …, "run": …}）
    origin: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: 新增它的那次修订
    created_rev_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: 删除它的那次修订、那次修订的客户端时间（与后到的写入比较先后，§3.5）
    retired_rev_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 取代它的条目编号。删除了但为空 = 被遗忘（只删除、没有新增）
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active','deleted')", name="status"),
        UniqueConstraint("instance_uuid", "number"),
        Index("ix_mem_entry_topic", "topic_uuid", "status"),
    )


class Revision(_Row, Base):
    """一次修订。created_at = 入库时间。"""

    __tablename__ = "mem_revision"

    instance_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 写入 / 移动的目标主题编码（当时的）
    topic_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 这次删除的条目编号；新增（或恢复）的条目编号
    removed: Mapped[list[int]] = mapped_column(JSONType, default=list, nullable=False)
    added: Mapped[list[int]] = mapped_column(JSONType, default=list, nullable=False)
    #: 写入：客户端给的时间；撤销、恢复、移动、拆分：服务端时间
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 移动 / 拆分的细节（只有编号与主题编码，不含条目内容）
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    #: 写入的结果原样留一份：同一个幂等键重放时照它返回
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    undoes_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    undone_by_uuid: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    #: 客户端给的幂等键：网络重试不会写出两条
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('write','undo','restore','move','split')", name="kind"),
        Index("ix_mem_revision_instance", "instance_uuid", "id"),
        Index(
            "ux_mem_revision_idem",
            "instance_uuid",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
