"""Atlas 业务表（文档 §5.1）。

注意两件事：
  1. 本项目没有接 checkpointer，历史每轮从 message 表重建（见 services/compaction.py）。
     所以这里就是全部的持久化 schema，没有另一半藏在 LangGraph 自建表里。
  2. message 表永远保留完整原文；上下文压缩只改写重建时的输入（文档 §7.4）。

**列类型一律走 db/types.py**，不直接引 `sqlalchemy.dialects.postgresql` ——
schema 定义里出现厂商名，整张表就和那一家焊死了。

**不建外键**（迁移 0013 起）：引用完整性与级联删除由代码逻辑负责，表上只保留
引用列与索引。新增表、新增列时不要再写 `ForeignKey(...)`。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
    """用户中心用户在 Atlas 的投影（权限设计 §5）。

    ★ id 直接用用户中心的用户 id：created_by、对象存储路径、Pod 配额标签、记忆的 user_id
      都是它，不再映射。资料以用户中心为准，登录与授权快照刷新时同步；角色不存库。
    表名不能用 "user" —— 那是 SQL 保留字。
    """

    __tablename__ = "app_user"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # String 而非 Text：它进了唯一索引，而 MySQL 不能给无长度的 TEXT 建索引
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    dept_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dept_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: active | disabled（停用的用户所有请求 401，执行器不再为他启动新的 run）
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
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


class AuthSession(Base):
    """浏览器登录会话（权限设计 §4.1）。Cookie 里只放随机令牌，这里存它的 SHA-256。"""

    __tablename__ = "auth_session"

    id: Mapped[UUID] = _uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )


class ServiceAccount(Base):
    """服务账号（权限设计 §10）：后端服务（如 AI TeamFlow）调用 Atlas 的主体。

    不是用户中心的人员，所以不能被授予数据权限：可用智能体范围在这里配置。
    密钥只显示一次，这里只存 SHA-256。
    """

    __tablename__ = "service_account"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    operations: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: all_enabled | list
    agent_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="all_enabled"
    )
    agent_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: 是否接受「代表用户」（X-Atlas-Acting-User）：只用于列表取交集与审计
    allow_acting: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: 该账号所建会话的用户记忆默认值；默认关闭，避免同一服务身份下的数据互相串味
    memory_default: Mapped[str] = mapped_column(String(8), nullable=False, server_default="off")
    #: active | disabled
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("agent_scope IN ('all_enabled','list')", name="agent_scope_enum"),
        CheckConstraint("memory_default IN ('on','off')", name="memory_default_enum"),
        CheckConstraint("status IN ('active','disabled')", name="status_enum"),
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
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )

    __table_args__ = (
        CheckConstraint("status IN ('draft','enabled','archived')", name="status_enum"),
    )


class AgentVersion(Base):
    """★ 配置版本快照：run 必须能指回"当时用的哪份配置"（文档 §5.4）。"""

    __tablename__ = "agent_version"

    id: Mapped[UUID] = _uuid_pk()
    agent_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)


class Thread(Base, TimestampMixin):
    __tablename__ = "thread"

    id: Mapped[UUID] = _uuid_pk()
    agent_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
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
        UUIDType, nullable=True
    )
    #: 与 parent_thread_id 组成恢复的查找键（唯一）。
    subagent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ACP 侧的会话标识，供 loadSession 恢复（acp 详设那一期接入）。
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: git 凭据档案（cluster 配置里的名字）：acp 会话的 Pod 据此挂载 git 凭据。
    #: 建会话时由调用方声明（如 AI TeamFlow 给节点会话声明产物仓库的档案）；子会话继承父会话。
    git_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 所有者 = created_by；所有者是用户（user）还是服务账号（service）。子会话继承父会话
    owner_kind: Mapped[str] = mapped_column(String(8), nullable=False, server_default="user")
    #: 用户记忆：on | off。off = 不抽取、不注入、不挂检索工具（服务账号的会话默认 off）
    memory: Mapped[str] = mapped_column(String(8), nullable=False, server_default="on")
    created_by: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )

    @property
    def root_thread_id(self) -> UUID:
        """会话树的根。委派深度封顶 1，所以一层 `or` 就到顶。

        ★ 两件事挂在它上面，而它们是不同的关切：
            工作区   子智能体读写父会话的 /workspace
            事件流   一个会话一条流，子 run 的事件也进根会话那条
          共用同一个派生值是巧合（都是「根」），不是耦合 —— 所以各自有名字。
        """
        return self.parent_thread_id or self.id

    @property
    def workspace_thread_id(self) -> UUID:
        """工作区归属的会话 —— 子智能体挂的是**父会话的** workspace。

        ★ 派生而不入库：加第四列反而引入不一致的可能（两处都能改，
          而它们必须永远相等）。skills / system 仍按 `id` 走，所以
          「读共享、技能隔离」是结构性的，不靠约定。
        """
        return self.root_thread_id

    @property
    def stream_thread_id(self) -> UUID:
        """事件流归属的会话。

        ★ 子 run 的事件必须进**根会话**那条流，否则「一个会话看到所有事」
          这条承诺不成立 —— 子 run 活在子会话上，它的流会变成一条谁也不
          订阅的孤流（这正是审批弹窗在委派挂起期间消失的根因，
          doc/detail/suspension.html §06）。
        """
        return self.root_thread_id

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
    """存 Anthropic content blocks 原始结构。永远保留完整原文（文档 §7.4）。

    ★ 一轮的**中间消息**也在这里：模型发出的 tool_use（assistant 的 block）
      与每个工具的结果（user 的 block）。映射归 domain/messages.py ——
      Anthropic 的 tool_result 本来就是 user 角色，所以 role 只有两个值。
    """

    __tablename__ = "message"

    id: Mapped[UUID] = _uuid_pk()
    thread_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )
    run_id: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 'chat' = 对话轮（用户提问 / 助手回答，后者可带 tool_use）；
    #: 'tool_result' = 一批工具结果（role 同为 'user'，靠本列区分）。
    #:
    #: ★ 没有它，「最后一条 user 消息是本轮输入」这条判据就会把一批工具
    #:   结果当成用户的新提问（迁移 0009）。
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="chat")
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
        CheckConstraint("kind IN ('chat','tool_result')", name="kind_enum"),
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
        UUIDType, nullable=False
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )
    #: 发起这次委派的 run（迁移 0007）。NULL = 用户直接发起的 run。
    #: ★ 子 run 是独立可寻址的执行记录，父行没了不该让它的用量统计跟着消失。
    #:   准入控制按它数「本 run 累计委派几次」。
    parent_run_id: Mapped[UUID | None] = mapped_column(
        UUIDType, nullable=True
    )
    #: 发起人（权限设计 §11）：用户本人，或服务账号代表的用户；系统投递为空。
    #: 与会话所有者不同 —— 服务账号的会话归服务账号，这一轮是谁触发的记在这里。
    created_by: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    #: 挂起时「在等什么」（迁移 0012）。形如
    #: [{"reason": "approval", "token": "<approval_id>"}]。
    #:
    #: ★ 没有它，续跑的 barrier 会判错方向：委派的 barrier 是「子 run 终态」，
    #:   而审批挂起时**没有子 run** —— barrier 立刻满足，于是续跑、又挂起、
    #:   再续跑，死循环。两种 reason 的等待条件不同，必须显式记下来。
    waiting_on: Mapped[list | None] = mapped_column(JSONType, nullable=True)
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
            postgresql_where=text(
                "status IN ('queued','running','awaiting_approval','suspended')"
            ),
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled',"
            "'interrupted','awaiting_approval','suspended')",
            name="status_enum",
        ),
    )


class RunEvent(Base):
    """轨迹事件归档。Redis Stream 是实时通道，这里是历史（文档 §10.2）。

    ★ 主键是 `(thread_id, thread_seq)` 而不是 `(run_id, seq)`（迁移 0011）。
      订阅单位从 run 换成 thread 之后，回放的查询是「这个会话里 thread_seq
      之后的事件」—— 跨 run 的，包括子 run 的。按 run 查轨迹仍然支持，靠
      `run_id` 上的普通索引。

    ★ `message.delta` 不进这张表（见 executor 的 _ARCHIVED 过滤）。delta 的
      价值是实时打字效果；回放历史时正文来自 message.completed 一次到位。
      不滤的话一个长会话能到几十万行，而其中绝大多数永远不会被读。
    """

    __tablename__ = "run_event"

    #: 事件流归属的会话 = 根会话（Thread.stream_thread_id）。子 run 的事件
    #: 写的是**父会话**的 thread_id —— 这是「一个会话一条流」的落地。
    thread_id: Mapped[UUID] = mapped_column(
        UUIDType, primary_key=True
    )
    #: 会话内单调递增（契约规则 2 的新口径）。由 Redis INCR 分配，见 EventRelay。
    thread_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )
    #: run 内单调递增。过渡期前端仍用它做游标；thread 流切换后只剩诊断价值。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)

    __table_args__ = (Index("ix_run_event_run", "run_id", "seq"),)


class RunFile(Base):
    """虚拟文件系统产物。MVP 直接入库；>1MB 时改对象存储（风险 R9）。"""

    __tablename__ = "run_file"

    id: Mapped[UUID] = _uuid_pk()
    thread_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        UUIDType, nullable=False
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
        UUIDType, nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args: Mapped[dict] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    #: 触发这次审批的工具调用（迁移 0012）。审计用 —— approval.id 本身就由
    #: (run_id, tool_call_id) 派生，存一份省去排查时反推。
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_by: Mapped[UUID | None] = mapped_column(
        UUIDType, nullable=True
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


class SkillUsageDaily(Base):
    """技能的使用量，按天汇总（技能 / MCP 设计 §7.4，迁移 0014）。

    「选中」= 模型读了 SKILL.md（skill.loaded 事件）。run 进入终态时从该 run
    的事件归档里汇总一次 —— 跨段的 run 也只算一次。

    ★ 归运行时：这是运行数据，不是配置。配置服务不知道谁在用哪个技能。
    """

    __tablename__ = "skill_usage_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(UUIDType, primary_key=True)
    #: 加载过这个技能的 run 数（同一 run 多次加载只算一次）
    loads: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: 其中正常结束（succeeded）的 run 数
    completed_runs: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
