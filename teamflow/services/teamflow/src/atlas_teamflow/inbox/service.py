"""InboxService：待办与通知的唯一入口（c4-model/teamflow/code-inbox.html）。

等你        现算：评审名单里有我、我还没表态的评审（数字员工的提问、审批在节点页对话里以卡片出现，
            不进这里）
站内通知    通知器按流程事件生成（notifier.py）；这里列、标已读
渠道        团队开哪些渠道（团队 Owner），个人绑定哪个账号（本人）；两者都有才推
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.types import utcnow
from ..errors import Invalid
from ..process.service import ProcessService
from .models import CHANNELS, ChannelBinding, Notification, TeamChannel

__all__ = ["InboxService", "Todo"]


@dataclass(frozen=True)
class Todo:
    #: admit / exit
    kind: str
    process_uuid: UUID
    process_no: int
    process_title: str
    project_uuid: UUID
    team_uuid: UUID
    node_id: str
    node_name: str
    since: datetime


class InboxService:
    def __init__(self, session: AsyncSession, process: ProcessService) -> None:
        self.session = session
        self.process = process

    async def todos(self, uid: UUID, *, project_uuid: UUID | None = None) -> list[Todo]:
        out = []
        for proc, node, rr in await self.process.open_reviews_for(uid, project_uuid=project_uuid):
            out.append(
                Todo(
                    kind=rr.stage,
                    process_uuid=proc.uuid,
                    process_no=proc.no,
                    process_title=proc.title,
                    project_uuid=proc.project_uuid,
                    team_uuid=proc.team_uuid,
                    node_id=node.node_id,
                    node_name=node.name,
                    since=rr.created_at,
                )
            )
        return out

    # ───────────────────────────── 站内通知

    async def notifications(
        self, uid: UUID, *, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        stmt = select(Notification).where(Notification.uid == uid)
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return list(await self.session.scalars(stmt.order_by(Notification.id.desc()).limit(limit)))

    async def unread(self, uid: UUID) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.uid == uid, Notification.read_at.is_(None))
            )
            or 0
        )

    async def mark_read(self, uid: UUID, ids: list[UUID] | None) -> None:
        """ids 为空 = 全部标已读。"""
        stmt = update(Notification).where(Notification.uid == uid, Notification.read_at.is_(None))
        if ids:
            stmt = stmt.where(Notification.uuid.in_(ids))
        await self.session.execute(stmt.values(read_at=utcnow()))

    # ───────────────────────────── 渠道

    async def team_channels(self, team_uuid: UUID) -> TeamChannel:
        row = await self.session.scalar(
            select(TeamChannel).where(TeamChannel.team_uuid == team_uuid)
        )
        return row or TeamChannel(
            team_uuid=team_uuid, wecom=False, dingtalk=False, email_digest=True
        )

    async def set_team_channels(
        self, team_uuid: UUID, *, wecom: bool, dingtalk: bool, email_digest: bool
    ) -> TeamChannel:
        row = await self.session.scalar(
            select(TeamChannel).where(TeamChannel.team_uuid == team_uuid)
        )
        if row is None:
            row = TeamChannel(team_uuid=team_uuid)
            self.session.add(row)
        row.wecom, row.dingtalk, row.email_digest = wecom, dingtalk, email_digest
        await self.session.flush()
        return row

    async def bindings(self, uid: UUID) -> list[ChannelBinding]:
        return list(
            await self.session.scalars(select(ChannelBinding).where(ChannelBinding.uid == uid))
        )

    async def bind(self, uid: UUID, channel: str, account: str) -> ChannelBinding:
        if channel not in CHANNELS:
            raise Invalid("VALIDATION_FAILED", "渠道只能是 wecom、dingtalk、email")
        account = (account or "").strip()
        if not account or len(account) > 128:
            raise Invalid("VALIDATION_FAILED", "账号必填，最多 128 个字符")
        row = await self.session.scalar(
            select(ChannelBinding).where(
                ChannelBinding.uid == uid, ChannelBinding.channel == channel
            )
        )
        if row is None:
            row = ChannelBinding(uid=uid, channel=channel, account=account)
            self.session.add(row)
        row.account = account
        await self.session.flush()
        return row

    async def unbind(self, uid: UUID, channel: str) -> None:
        row = await self.session.scalar(
            select(ChannelBinding).where(
                ChannelBinding.uid == uid, ChannelBinding.channel == channel
            )
        )
        if row is not None:
            await self.session.delete(row)
            await self.session.flush()
