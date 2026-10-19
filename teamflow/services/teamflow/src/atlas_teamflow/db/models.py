"""TeamFlow 的表（配置侧：文件模板、流程模板、团队、项目）。

主键约定：`id` 自增主键只在库内使用；`uuid` 逻辑主键用于全部关联与 API。
人员一律用用户中心的用户 uuid 引用（tf_user 只是本地缓存）；团队成员与级别不在本库，
存于用户中心数据权限（数据编码 Team，权限设计 §07）。
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
    MetaData,
    String,
    Text,
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


# ───────────────────────────────────────────── 身份


class TfUser(Keys, Base):
    """用户中心用户的本地缓存。★ uuid 就是用户中心的用户 uuid（全系统稳定标识）。"""

    __tablename__ = "tf_user"

    account: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ───────────────────────────────────────────── 文件模板


class FileTemplate(Keys, Audited, Base):
    __tablename__ = "tf_file_template"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    icon: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[str] = mapped_column(String(16), default="artifact", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    current_version_uuid: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (
        CheckConstraint("usage IN ('artifact','review_rule','other')", name="usage_enum"),
        CheckConstraint("status IN ('active','disabled')", name="status_enum"),
    )


class FileTemplateVersion(Keys, Base):
    """不可变：只插入不更新（文件模板设计 §06）。"""

    __tablename__ = "tf_file_template_version"

    template_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_file_template.uuid"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_by: Mapped[UUID | None] = mapped_column(UUIDType)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("template_uuid", "version_no", name="uq_tf_ftv_no"),)


# ───────────────────────────────────────────── 流程模板


class FlowTemplate(Keys, Audited, Base):
    __tablename__ = "tf_flow_template"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    icon: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str | None] = mapped_column(String(200))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    current_version_uuid: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (CheckConstraint("status IN ('active','disabled')", name="status_enum"),)


class FlowTemplateVersion(Keys, Base):
    """不可变：发布后定义只读（流程模板设计 §07）。"""

    __tablename__ = "tf_flow_template_version"

    template_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_flow_template.uuid"), nullable=False)
    major: Mapped[int] = mapped_column(Integer, nullable=False)
    minor: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: 发布时推导：{"exec": [...], "review": [...]}
    roles: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    change_note: Mapped[str] = mapped_column(String(500), nullable=False)
    published_by: Mapped[UUID | None] = mapped_column(UUIDType)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("template_uuid", "major", "minor", name="uq_tf_flow_version"),
    )


class FlowTemplateDraft(Keys, Base):
    """一模板一草稿。"""

    __tablename__ = "tf_flow_template_draft"

    template_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_flow_template.uuid", ondelete="CASCADE"), unique=True, nullable=False
    )
    base_version_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    editor_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class FlowRole(Keys, Base):
    """流程角色库（执行角色 / 评审角色的名称库）。"""

    __tablename__ = "tf_flow_role"

    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


# ───────────────────────────────────────────── 团队与项目


class Team(Keys, Audited, Base):
    """★ 成员与团队管理员不在本表：用户中心数据权限 Team（成员读写、管理员 Owner）。"""

    __tablename__ = "tf_team"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    __table_args__ = (CheckConstraint("status IN ('active','archived')", name="status_enum"),)


class TeamAgent(Keys, Base):
    """团队接入的 Atlas Agent：只存引用与快照，能力以 Atlas 为准（团队设计 §07）。"""

    __tablename__ = "tf_team_agent"

    team_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_team.uuid"), nullable=False)
    atlas_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_key: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    added_by: Mapped[UUID | None] = mapped_column(UUIDType)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("team_uuid", "atlas_agent_id", name="uq_tf_team_agent"),)


class Project(Keys, Audited, Base):
    __tablename__ = "tf_project"

    team_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_team.uuid"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    #: 绑定模板 id，跟随模板当前版本（团队设计 §8.1）
    flow_template_uuid: Mapped[UUID | None] = mapped_column(ForeignKey("tf_flow_template.uuid"))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    #: 产物仓库名（命名空间由配置决定）；建仓库失败时 repo_status=error，可重试
    repo_name: Mapped[str | None] = mapped_column(String(100))
    #: none | ready | error
    repo_status: Mapped[str] = mapped_column(String(8), default="none", nullable=False)
    repo_error: Mapped[str | None] = mapped_column(String(300))
    #: 允许团队管理员用自己的 Gitee 账号直接提交（默认否：产物由 Agent 提交）
    allow_admin_commit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("team_uuid", "name", name="uq_tf_project_name"),
        CheckConstraint("status IN ('active','archived')", name="status_enum"),
    )


class RoleAssignment(Keys, Base):
    """项目里的流程角色分配：流程角色 → 成员（用户中心用户 uuid）/ 团队 Agent。"""

    __tablename__ = "tf_project_role_assignment"

    project_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_project.uuid", ondelete="CASCADE"), nullable=False
    )
    role_name: Mapped[str] = mapped_column(String(32), nullable=False)
    assignee_type: Mapped[str] = mapped_column(String(8), nullable=False)
    assignee_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "project_uuid", "role_name", "assignee_type", "assignee_uuid", name="uq_tf_assignment"
        ),
        CheckConstraint("assignee_type IN ('user','agent')", name="assignee_type_enum"),
        Index("ix_tf_assignment_project_role", "project_uuid", "role_name"),
    )


# ───────────────────────────────────────────── 流程运行


class Process(Keys, Base):
    """一次流程：按项目绑定的模板发起，发起时锁定模板版本（系统设计 §12）。"""

    __tablename__ = "tf_process"

    team_uuid: Mapped[UUID] = mapped_column(ForeignKey("tf_team.uuid"), nullable=False, index=True)
    project_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_project.uuid"), nullable=False, index=True
    )
    no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    template_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    template_version_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_flow_template_version.uuid"), nullable=False
    )
    #: running | completed | terminated
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 流程事件序号（事件表的游标），在流程行锁内递增
    event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_uuid", "no", name="uq_tf_process_no"),
        CheckConstraint("status IN ('running','completed','terminated')", name="status_enum"),
    )


class ProcessNode(Keys, Base):
    """流程中的节点。开工时锁定文件模板版本与执行 Agent；评审人名单在评审轮次上锁定。"""

    __tablename__ = "tf_process_node"

    process_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 冗余模板中的执行角色：「待我处理」按项目当前分配匹配人
    exec_role: Mapped[str] = mapped_column(String(32), nullable=False)
    #: pending | working | exit_review | exited（准出通过，待下游准入）
    #: | admit_review（本节点开工前的准入评审中） | passed | returned
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    #: 第几轮（每次回到人机协同 +1）
    round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_template_version_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    agent_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    atlas_agent_id: Mapped[str | None] = mapped_column(String(64))
    atlas_thread_id: Mapped[str | None] = mapped_column(String(64))
    #: Atlas 会话流的游标（thread_seq），断线 / 重启后从这里续传
    atlas_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 正在跑的 Atlas run；空 = Agent 空闲
    atlas_run_id: Mapped[str | None] = mapped_column(String(64))
    #: 提示（如「执行 Agent 不可用」「Agent 运行失败」），空 = 正常
    notice: Mapped[str | None] = mapped_column(String(200))
    current_artifact_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    #: 本节点的产物路径（首次开工时计算并固定；Agent 提交到这里，提交准出评审时锁定 commit）
    artifact_path: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    passed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("process_uuid", "node_id", name="uq_tf_process_node"),
        CheckConstraint(
            "status IN ('pending','working','exit_review','exited','admit_review','passed',"
            "'returned')",
            name="status_enum",
        ),
    )


class NodeMessage(Keys, Base):
    """人机协同区的消息：人的指令、Agent 的回复、系统提示。"""

    __tablename__ = "tf_node_message"

    node_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process_node.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    #: user | agent | system
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    author_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Agent 消息对应的 Atlas run
    atlas_run_id: Mapped[str | None] = mapped_column(String(64))
    #: done | streaming | failed
    status: Mapped[str] = mapped_column(String(12), default="done", nullable=False)
    round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class ArtifactVersion(Keys, Base):
    """产物版本：产物路径上的一次提交（由 Agent 或被授权的人提交，TeamFlow 观察登记）。
    内容在库里缓存一份供预览与审计；以仓库为准。"""

    __tablename__ = "tf_artifact_version"

    node_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process_node.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: agent（提交说明带 TeamFlow 尾注）| user（作者是已绑定账号的成员）| unknown
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 作者是已绑定 Gitee 账号的成员时记成员
    created_by: Mapped[UUID | None] = mapped_column(UUIDType)
    #: 提交说明的第一行
    note: Mapped[str | None] = mapped_column(String(200))
    author_name: Mapped[str | None] = mapped_column(String(100))
    author_login: Mapped[str | None] = mapped_column(String(100))
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    web_url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("node_uuid", "version_no", name="uq_tf_artifact_no"),
        # 观察幂等：同一个提交只登记一次（多实例 / 重复扫描）
        UniqueConstraint("node_uuid", "commit_sha", name="uq_tf_artifact_commit"),
    )


class ReviewRound(Keys, Base):
    """一轮评审：准出 = 本节点产物能否交出；准入 = 本节点开工前审核上游产物（挂在下游节点上）。"""

    __tablename__ = "tf_review_round"

    node_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process_node.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    #: exit | admit
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    #: any | all
    rule: Mapped[str] = mapped_column(String(4), nullable=False)
    #: 锁定的评审人（用户 uuid 字符串列表）
    reviewers: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    #: 准出：锁定的本节点产物版本；准入：空（审核的是上游各节点当前锁定的版本）
    artifact_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    #: open | passed | rejected | cancelled
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Vote(Keys, Base):
    __tablename__ = "tf_vote"

    review_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_review_round.uuid", ondelete="CASCADE"), nullable=False
    )
    user_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: approve | reject
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    #: 驳回时打回到的更早节点（node_id）；空 = 本节点修改
    return_to: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("review_uuid", "user_uuid", name="uq_tf_vote"),)


class ProcessEvent(Keys, Base):
    """流程事件：只追加。审计与实时推送（SSE 游标）的共同来源（系统设计 §9.2）。"""

    __tablename__ = "tf_process_event"

    process_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process.uuid", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(32))
    actor_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("process_uuid", "seq", name="uq_tf_process_event_seq"),)


# ───────────────────────────────────────────── 代码托管账号与仓库访问


class GitAccount(Keys, Base):
    """成员绑定的 Gitee 账号（OAuth 证明归属；不保存任何令牌）。"""

    __tablename__ = "tf_git_account"

    user_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_user.uuid", ondelete="CASCADE"), unique=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), default="gitee", nullable=False)
    login: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    bound_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_tf_git_account_provider"),
    )


class RepoCollaborator(Keys, Base):
    """TeamFlow 为成员设置的仓库协作者权限（期望值 + 最近一次同步结果）。对账以它为准，
    只管理自己设置过的账号，不碰命名空间所有者等其他成员。"""

    __tablename__ = "tf_repo_collaborator"

    project_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_project.uuid", ondelete="CASCADE"), nullable=False
    )
    login: Mapped[str] = mapped_column(String(100), nullable=False)
    user_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    #: pull | push
    permission: Mapped[str] = mapped_column(String(8), nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (UniqueConstraint("project_uuid", "login", name="uq_tf_repo_collaborator"),)


# ───────────────────────────────────────────── 审计


class AuditEvent(Keys, Base):
    __tablename__ = "tf_audit_event"

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    actor_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    target_name: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    __table_args__ = (
        Index("ix_tf_audit_event_time", text("occurred_at DESC")),
        Index("ix_tf_audit_event_target", "target_type", "target_uuid"),
    )
