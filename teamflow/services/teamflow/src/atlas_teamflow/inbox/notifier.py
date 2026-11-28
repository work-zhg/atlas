"""Notifier：按流程事件生成站内通知、推到渠道（定时任务，每个副本都跑）。

一轮：锁住游标行（锁不到就跳过，别的副本在推）→ 在系统范围里按全局 id 读游标之后的一批事件 →
每条进入它的租户，按 data.to 给每个人写一条站内通知（按（事件，人）唯一，重复处理不会多一条），
再按团队开的渠道和个人绑定的账号推送 → 推进游标。

★ 事件和状态在同一个事务里写，通知器按 id 往后读就不会漏；推送失败只记日志，不挡站内通知。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from atlas_tenancy import SystemReason, TenantScope
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..ids import uuid7
from ..process.models import Process, ProcessEvent, ProcessNode
from .channels import NotifyChannel
from .models import ChannelBinding, Notification, NotifyCursor, TeamChannel

__all__ = ["Notifier", "describe"]

log = logging.getLogger("atlas_teamflow.inbox")
CURSOR = "notifier"


def describe(ev: ProcessEvent, proc: Process, node: str) -> tuple[str, str] | None:
    """事件 → (通知文字, 前端链接)。不需要通知的事件返回 None。"""
    head = f"F-{proc.no} {proc.title}"
    link = f"/flows/{proc.uuid}" + (f"/n/{ev.node_id}" if ev.node_id else "")
    if ev.type == "review.opened":
        stage = "准入" if ev.data.get("stage") == "admit" else "准出"
        return f"{head} · 「{node}」等你{stage}评审", link
    if ev.type == "node.returned":
        comment = str(ev.data.get("comment") or "")[:80]
        return f"{head} · 「{node}」被退回，请按意见修改：{comment}", link
    if ev.type == "node.started":
        rnd = ev.data.get("round") or 1
        return f"{head} · 「{node}」" + ("开始了" if rnd <= 1 else f"开始第 {rnd} 轮"), link
    if ev.type == "process.completed":
        return f"{head} 已完成", link
    return None


class Notifier:
    def __init__(
        self,
        sessions: Callable[[], async_sessionmaker[AsyncSession]],
        channels: dict[str, NotifyChannel] | None = None,
        *,
        batch: int = 200,
    ) -> None:
        self._sessions = sessions
        self.channels = channels or {}
        self.batch = batch

    async def run(self) -> int:
        """处理一批；返回这批处理了几条事件。"""
        async with self._sessions()() as s:
            with TenantScope.system(SystemReason.SWEEP):
                await s.execute(
                    insert(NotifyCursor)
                    .values(name=CURSOR, last_event_id=0)
                    .on_conflict_do_nothing()
                )
                await s.commit()
                cursor = await s.scalar(
                    select(NotifyCursor)
                    .where(NotifyCursor.name == CURSOR)
                    .with_for_update(skip_locked=True)
                )
                if cursor is None:
                    return 0
                events = list(
                    await s.scalars(
                        select(ProcessEvent)
                        .where(ProcessEvent.id > cursor.last_event_id)
                        .order_by(ProcessEvent.id)
                        .limit(self.batch)
                    )
                )
            for ev in events:
                with TenantScope.enter(ev.tenant_uuid):
                    try:
                        await self._deliver(s, ev)
                    except Exception:
                        log.exception("事件 %s 的通知失败，跳过", ev.id)
            if events:
                cursor.last_event_id = events[-1].id
            await s.commit()
            return len(events)

    async def _deliver(self, s: AsyncSession, ev: ProcessEvent) -> None:
        to = [UUID(u) for u in ev.data.get("to") or []]
        if not to:
            return
        proc = await s.scalar(select(Process).where(Process.uuid == ev.process_uuid))
        if proc is None:
            return
        node = ""
        if ev.node_id:
            node = (
                await s.scalar(
                    select(ProcessNode.name).where(
                        ProcessNode.process_uuid == proc.uuid, ProcessNode.node_id == ev.node_id
                    )
                )
                or ev.node_id
            )
        got = describe(ev, proc, node)
        if got is None:
            return
        text, link = got
        for uid in dict.fromkeys(to):
            await s.execute(
                insert(Notification)
                .values(
                    uuid=uuid7(),
                    tenant_uuid=ev.tenant_uuid,
                    uid=uid,
                    text=text[:300],
                    link=link[:300],
                    event_id=ev.id,
                )
                .on_conflict_do_nothing(constraint="uq_tf_notification")
            )
        await self._push(s, proc.team_uuid, to, text, link)

    async def _push(
        self, s: AsyncSession, team_uuid: UUID, to: list[UUID], text: str, link: str
    ) -> None:
        if not self.channels:
            return
        team = await s.scalar(select(TeamChannel).where(TeamChannel.team_uuid == team_uuid))
        if team is None:
            return
        enabled = {c for c, on in (("wecom", team.wecom), ("dingtalk", team.dingtalk)) if on}
        enabled &= set(self.channels)
        if not enabled:
            return
        rows = await s.scalars(
            select(ChannelBinding).where(
                ChannelBinding.uid.in_(to), ChannelBinding.channel.in_(enabled)
            )
        )
        for b in rows:
            try:
                await self.channels[b.channel].send(b.account, text, link)
            except Exception as exc:
                log.warning("推送到 %s 失败：%s", b.channel, exc)
