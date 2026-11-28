"""待办与通知的表：tf_notification、tf_team_channel、tf_channel_binding、tf_notify_cursor。

★ tf_notify_cursor 不属于任何租户：通知器在系统范围里按全局事件 id 往后读，处理每条时再进入
  事件所属的租户。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import BigInteger, Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base, Keys, Stamped
from ..db.types import UTCDateTime, UUIDType

__all__ = ["CHANNELS", "ChannelBinding", "Notification", "NotifyCursor", "TeamChannel"]

#: 个人能绑定的渠道
CHANNELS = ("wecom", "dingtalk", "email")


class Notification(TenantOwned, Keys, Stamped, Base):
    """一条站内通知；按（事件，人）唯一，重复处理不会多一条。"""

    __tablename__ = "tf_notification"

    uid: Mapped[UUID] = mapped_column(UUIDType, nullable=False, index=True)
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    #: 前端路由，如 /flows/{流程}/n/{节点}
    link: Mapped[str] = mapped_column(String(300), nullable=False)
    #: 流程事件的全局 id
    event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("event_id", "uid", name="uq_tf_notification"),)


class TeamChannel(TenantOwned, Keys, Base):
    """一个团队打开了哪些渠道。"""

    __tablename__ = "tf_team_channel"

    team_uuid: Mapped[UUID] = mapped_column(UUIDType, unique=True, nullable=False)
    wecom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dingtalk: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_digest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ChannelBinding(TenantOwned, Keys, Base):
    """一个人在某个渠道上的账号；没绑的渠道不推给他。"""

    __tablename__ = "tf_channel_binding"

    uid: Mapped[UUID] = mapped_column(UUIDType, nullable=False)
    #: wecom / dingtalk / email
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 企业微信 userid、钉钉 userid、邮箱
    account: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (UniqueConstraint("uid", "channel", name="uq_tf_channel_binding"),)


class NotifyCursor(Base):
    """通知器的游标（全局一行）：读到了哪个流程事件 id。"""

    __tablename__ = "tf_notify_cursor"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
