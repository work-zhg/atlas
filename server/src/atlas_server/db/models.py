"""Atlas 业务表（文档 §5.1）。

注意两件事：
  1. 本项目没有接 checkpointer，历史每轮从 message 表重建（见 services/compaction.py）。
     所以这里就是全部的持久化 schema，没有另一半藏在 LangGraph 自建表里。
  2. message 表永远保留完整原文；上下文压缩只改写重建时的输入（文档 §7.4）。

**列类型一律走 db/types.py**，不直接引 `sqlalchemy.dialects.postgresql` ——
schema 定义里出现厂商名，整张表就和那一家焊死了。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .types import JSONType, UTCDateTime, UUIDType


def _uuid_pk() -> Mapped[UUID]:
    """主键默认值在 Python 侧生成 —— gen_random_uuid() 是 PG 内置函数，
    MySQL 没有对应物。副作用是好的：insert 之前就知道 id。"""
    return mapped_column(UUIDType, primary_key=True, default=uuid4)


class AppUser(Base):
    """决策 1：不做鉴权，只存 uid + name，后期关联外部系统 user 表。

    表名不能用 "user" —— 那是 SQL 保留字。
    """

    __tablename__ = "app_user"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # String 而非 Text：它进了唯一索引，而 MySQL 不能给无长度的 TEXT 建索引
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ux_app_user_external_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )


class ModelCatalog(Base):
    """网关 GET /v1/models 只给 id，能力元信息必须在这里维护（文档 §3 D2）。

    supports_temperature / min_cacheable_tokens 来自 2026-08-19 实测，
    不是推测：temperature 在 opus-5 上是硬 400。
    """

    __tablename__ = "model_catalog"

    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    supports_thinking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ★ 实测：haiku-4-5 传 adaptive 会 400，它只有旧式 budget_tokens 思考。
    #   与 supports_thinking 是两回事，编辑器要据此决定是否显示"思考"开关。
    supports_adaptive_thinking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ★ 实测：haiku-4-5 传 output_config.effort 会 400
    supports_effort: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_cache: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ★ 实测：opus-5 / sonnet-5 / fable-5 返回 400，仅 haiku-4-5 接受
    supports_temperature: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ★ prompt 缓存最小可缓存前缀，非单调：opus-5=512 / sonnet-5=1024 / haiku-4-5=4096
    min_cacheable_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("context_window > 0", name="context_window_positive"),
        CheckConstraint("max_output_tokens > 0", name="max_output_positive"),
    )


class Agent(Base, TimestampMixin):
    __tablename__ = "agent"

    id: Mapped[UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # TEXT 列的默认值挪到 Python 侧：MySQL 不接受 TEXT/JSON 的字面量默认值（1101）
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    avatar_key: Mapped[str] = mapped_column(String(32), nullable=False, server_default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    current_version_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("agent_version.id", use_alter=True, name="fk_agent_current_version"),
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("app_user.id"), nullable=False
    )

    __table_args__ = (
        CheckConstraint("status IN ('draft','enabled','archived')", name="status_enum"),
    )


class AgentVersion(Base):
    """★ 配置版本快照：run 必须能指回"当时用的哪份配置"（文档 §5.4）。"""

    __tablename__ = "agent_version"

    id: Mapped[UUID] = _uuid_pk()
    agent_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("app_user.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)


class Thread(Base, TimestampMixin):
    __tablename__ = "thread"

    id: Mapped[UUID] = _uuid_pk()
    agent_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("agent.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 决策 5：pending|generated|fallback|manual。manual 永不被自动覆盖。
    title_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # 冗余快照（文档 §6 纪律 2）：可从事件流重建，丢了不影响正确性。
    latest_state: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    compact_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # §7.4：压缩后的摘要与其覆盖边界。message 表不受影响 ——
    # 模型视角被压缩，用户视角完整保留（迁移 0005）。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_upto: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # ── 子会话（迁移 0007）─────────────────────────────────────────
    # NULL = 用户会话；非空 = 某个会话的子智能体会话。子会话不出现在
    # 会话列表里 —— 它是父会话的一部分，不是用户的一段独立对话。
    parent_thread_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("thread.id", ondelete="CASCADE"), nullable=True
    )
    #: 与 parent_thread_id 组成恢复的查找键（唯一）。
    subagent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ACP 侧的会话标识，供 loadSession 恢复（acp 详设那一期接入）。
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("app_user.id"), nullable=False
    )

    @property
    def workspace_thread_id(self) -> UUID:
        """工作区归属的会话 —— 子智能体挂的是**父会话的** workspace。

        ★ 派生而不入库：加第四列反而引入不一致的可能（两处都能改，
          而它们必须永远相等）。skills / system 仍按 `id` 走，所以
          「读共享、技能隔离」是结构性的，不靠约定。
        """
        return self.parent_thread_id or self.id

    __table_args__ = (
        Index("ix_thread_list", "status", text("updated_at DESC")),
        # ★ 恢复的查找键。条件里的 status='active' 是关键的一半：
        #   一次性（ephemeral）子会话以 status='ephemeral' 出生，因此不占这个
        #   键 —— 同一个一次性子智能体于是可以**真并行**（设计 §09），
        #   而持久子智能体的同名委派仍被唯一约束 + 会话串行锁按住。
        Index(
            "ux_thread_subagent",
            "parent_thread_id",
            "subagent_name",
            unique=True,
            postgresql_where=text("subagent_name IS NOT NULL AND status = 'active'"),
        ),
        CheckConstraint(
            "title_source IN ('pending','generated','fallback','manual')",
            name="title_source_enum",
        ),
    )


class Message(Base):
    """存 Anthropic content blocks 原始结构。永远保留完整原文（文档 §7.4）。"""

    __tablename__ = "message"

    id: Mapped[UUID] = _uuid_pk()
    thread_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("thread.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[list] = mapped_column(JSONType, nullable=False)
    # ★ 每行必须拿到**互不相同**的时间。消息列表按 (created_at DESC, id DESC) 排序，
    #   并列时退化为按随机 UUID 排 —— 聊天记录顺序变成任意的（见迁移 0003）。
    #
    #   原来用 PG 的 clock_timestamp()（now() 返回的是事务开始时间，同一事务内并列）。
    #   改成 Python 侧生成：clock_timestamp() 是 PG 独有的，而 MySQL 的
    #   CURRENT_TIMESTAMP 虽是每语句取值，默认精度却只到**秒**，同一秒内照样并列。
    #   Python 的 datetime 是微秒精度且逐行求值，两个库上都成立。
    #
    #   代价是时间由应用时钟给出：多实例部署时理论上有时钟偏移。可接受 ——
    #   同一会话的 run 本来就被 Redis 锁串行化，不会有两个实例同时写一条线程。
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        Index("ix_message_thread", "thread_id", "created_at"),
        CheckConstraint("role IN ('user','assistant')", name="role_enum"),
    )


class Run(Base):
    """一次对话轮次。

    ★ thread_id 与 agent_version_id 都非空（迁移 0006）：试跑移除之后，
      run 必然属于某个会话、必然指向一份已保存的配置版本。可复现性
      （§5.4「run 能回答当时用的哪份配置」）因此由外键而非约定保证。
    """

    __tablename__ = "run"

    id: Mapped[UUID] = _uuid_pk()
    thread_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("thread.id", ondelete="CASCADE"), nullable=False
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("agent_version.id"), nullable=False
    )
    #: 发起这次委派的 run（迁移 0007）。NULL = 用户直接发起的 run。
    #: ★ 不做 CASCADE：子 run 是独立可寻址的执行记录，父行没了不该让
    #:   它的用量统计跟着消失。准入控制按它数「本 run 累计委派几次」。
    parent_run_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("run.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    error_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # 网关在 usage.output_tokens_details.thinking_tokens 上报（实测可用）
    thinking_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_run_thread", "thread_id", text("created_at DESC")),
        Index("ix_run_parent", "parent_run_id"),
        Index(
            "ix_run_active",
            "status",
            postgresql_where=text("status IN ('queued','running','awaiting_approval')"),
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled',"
            "'interrupted','awaiting_approval')",
            name="status_enum",
        ),
    )


class RunEvent(Base):
    """轨迹事件归档。Redis Stream 是实时通道，这里是历史（文档 §10.2）。"""

    __tablename__ = "run_event"

    run_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("run.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)


class RunFile(Base):
    """虚拟文件系统产物。MVP 直接入库；>1MB 时改对象存储（风险 R9）。"""

    __tablename__ = "run_file"

    id: Mapped[UUID] = _uuid_pk()
    thread_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("thread.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    # 同理：它是 ix_run_file_thread 的一部分
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_run_file_thread", "thread_id", "path", text("created_at DESC")),)


class Approval(Base):
    """人工确认（文档 §12.2）。拒绝不终止 run，作为工具结果回给 agent。"""

    __tablename__ = "approval"

    id: Mapped[UUID] = _uuid_pk()
    run_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args: Mapped[dict] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    decided_by: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("app_user.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired')", name="status_enum"
        ),
    )
