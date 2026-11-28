"""节点协作的表：tf_node_session、tf_node_instruction。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, Keys, Stamped
from ..db.types import UTCDateTime, UUIDType

__all__ = ["Instruction", "NodeSession"]


class NodeSession(TenantOwned, Keys, Stamped, Base):
    """一个节点的会话：第一次投递时在 Atlas 建（挂流程空间），之后每轮复用；记最后开的 run，
    停下时取消它；状态决定还能不能开新 run。"""

    __tablename__ = "tf_node_session"

    node_uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False)
    process_uuid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    atlas_thread_id: Mapped[str | None] = mapped_column(String(64))
    atlas_agent_id: Mapped[str | None] = mapped_column(String(64))
    #: 最后开的 run，停下时取消它
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    #: 提交后要取消 last_run_id，取消成功清掉
    cancel_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: active / taken_over / frozen / stopped
    state: Mapped[str] = mapped_column(String(12), default="active", nullable=False)
    taken_by: Mapped[UUID | None] = mapped_column(UUIDType)

    __table_args__ = (
        CheckConstraint("state IN ('active','taken_over','frozen','stopped')", name="state_enum"),
    )


class Instruction(TenantOwned, Keys, Stamped, Base):
    """一条指令，对应 Atlas 的一个 run。开 run 时的幂等键是本行 uuid。"""

    __tablename__ = "tf_node_instruction"

    session_uuid: Mapped[UUID] = mapped_column(
        ForeignKey("tf_node_session.uuid", ondelete="CASCADE"), nullable=False
    )
    #: kickoff / revise / human / handback
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 人发的、交还的；系统指令为空
    author: Mapped[UUID | None] = mapped_column(UUIDType)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: queued / sent / cancelled
    status: Mapped[str] = mapped_column(String(10), default="queued", nullable=False)
    atlas_run_id: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 投递失败的最近一条原因（排着重试时给人看）
    last_error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        CheckConstraint("kind IN ('kickoff','revise','human','handback')", name="kind_enum"),
        CheckConstraint("status IN ('queued','sent','cancelled')", name="status_enum"),
        Index("ix_tf_node_instruction_queue", "session_uuid", "status", "id"),
    )
