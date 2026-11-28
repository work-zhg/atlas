"""流程运行的表：tf_process、tf_process_node、tf_submission、tf_review_round、tf_vote、
tf_process_event。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, Keys, Stamped
from ..db.types import JSONType, UTCDateTime, UUIDType

__all__ = [
    "NODE_STATUSES",
    "Process",
    "ProcessEvent",
    "ProcessNode",
    "ReviewRound",
    "Submission",
    "Vote",
]

#: 节点的七种状态
NODE_STATUSES = (
    "pending",  # 待开始
    "working",  # 人机协同中
    "exit_review",  # 准出评审中
    "exited",  # 待下游准入
    "admit_review",  # 准入评审中
    "passed",  # 已通过
    "returned",  # 已打回
)


class Process(TenantOwned, Keys, Stamped, Base):
    """一条流程：编号在租户内递增（F-1024）；发起时锁模板版本、绑定流程空间。"""

    __tablename__ = "tf_process"

    no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    team_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    project_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    template_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    template_version_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    space_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 流程内事件序号，在流程行锁内递增
    event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_uuid", "no", name="uq_tf_process_no"),
        CheckConstraint("status IN ('running','completed','terminated')", name="status_enum"),
    )


class ProcessNode(TenantOwned, Keys, Base):
    """流程里的一个节点：状态和轮次；数字员工在第一次开工时锁定，之后每轮都是它。"""

    __tablename__ = "tf_process_node"

    process_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    exec_role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    #: 第几轮（每次回到人机协同 +1）
    round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 执行的数字员工（Atlas 智能体编号），第一次开工时锁定
    atlas_agent_id: Mapped[str | None] = mapped_column(String(64))
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


class Submission(TenantOwned, Keys, Stamped, Base):
    """一次提交：第几轮、从流程空间选了哪些文件。只记路径，不存内容、不做版本。"""

    __tablename__ = "tf_submission"

    node_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process_node.uuid", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    paths: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    submitted_by: Mapped[UUID] = mapped_column(UUIDType, nullable=False)

    __table_args__ = (UniqueConstraint("node_uuid", "round", name="uq_tf_submission"),)


class ReviewRound(TenantOwned, Keys, Stamped, Base):
    """一轮评审：准出审本节点这一轮的提交，准入审上游各自最近一次的提交（挂在下游节点上）。"""

    __tablename__ = "tf_review_round"

    node_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process_node.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    #: exit / admit
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    #: any / all
    rule: Mapped[str] = mapped_column(String(4), nullable=False)
    #: 开评审时锁定的评审人（uid 字符串）
    reviewers: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    #: 审的提交（submission uuid 字符串）
    submissions: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    #: open / approved / rejected / cancelled
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("stage IN ('exit','admit')", name="stage_enum"),
        CheckConstraint("status IN ('open','approved','rejected','cancelled')", name="status_enum"),
    )


class Vote(TenantOwned, Keys, Stamped, Base):
    __tablename__ = "tf_vote"

    review_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_review_round.uuid", ondelete="CASCADE"), nullable=False
    )
    reviewer: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: approve / reject
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    #: 打回到哪个祖先节点（node_id）
    return_to: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (UniqueConstraint("review_uuid", "reviewer", name="uq_tf_vote"),)


class ProcessEvent(TenantOwned, Keys, Stamped, Base):
    """流程事件：只追加。追溯、实时推送（流程内 seq 作游标）、待办与通知（全局 id 作游标）
    的来源。"""

    __tablename__ = "tf_process_event"

    process_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_process.uuid", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(32))
    #: 人；数字员工、系统为空
    actor: Mapped[UUID | None] = mapped_column(UUIDType)
    #: 摘要，和 to（该告诉谁：uid 字符串列表）
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    __table_args__ = (UniqueConstraint("process_uuid", "seq", name="uq_tf_process_event"),)
