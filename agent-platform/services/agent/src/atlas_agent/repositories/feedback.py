"""回答反馈（有帮助 / 没帮助）：唯一接触 message_feedback 表的地方。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Message, MessageFeedback


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def assistant_message(self, thread_id: UUID, message_id: UUID) -> Message | None:
        """这条会话里的一条助手消息；不是助手消息、或不属于这条会话 → None。"""
        return (
            await self._session.execute(
                select(Message).where(
                    Message.message_uuid == message_id,
                    Message.thread_uuid == thread_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one_or_none()

    async def upsert(
        self,
        message: Message,
        *,
        user_id: UUID,
        rating: str,
        reasons: list[str],
        comment: str,
    ) -> MessageFeedback:
        """一个人对一条回答只有一条反馈：有就改，没有就建。"""
        row = (
            await self._session.execute(
                select(MessageFeedback).where(
                    MessageFeedback.message_uuid == message.message_uuid,
                    MessageFeedback.user_uuid == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = MessageFeedback(
                message_uuid=message.message_uuid,
                thread_uuid=message.thread_uuid,
                run_uuid=message.run_uuid,
                user_uuid=user_id,
            )
            self._session.add(row)
        row.rating, row.reasons, row.comment = rating, reasons, comment
        await self._session.flush()
        return row

    async def remove(self, message_id: UUID, *, user_id: UUID) -> None:
        await self._session.execute(
            delete(MessageFeedback).where(
                MessageFeedback.message_uuid == message_id, MessageFeedback.user_uuid == user_id
            )
        )

    async def of_thread(self, thread_id: UUID, *, user_id: UUID) -> list[MessageFeedback]:
        return list(
            (
                await self._session.execute(
                    select(MessageFeedback).where(
                        MessageFeedback.thread_uuid == thread_id,
                        MessageFeedback.user_uuid == user_id,
                    )
                )
            ).scalars()
        )
