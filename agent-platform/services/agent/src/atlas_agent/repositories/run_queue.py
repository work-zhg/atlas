"""run 表上与派发有关的读写（Run 执行器 · 派发与租约，code-executor-scheduling.html）。

按 run 号抢锁（CAS 写租约）、续租、交接、把挂起的 run 放回排队、给兜底任务的候选。
锁就是 run 行上的租约（owner + lease_until），不另用 Redis 锁：写进度时本来就要按它挡住
旧实例（围栏），两把锁会说法不一。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Run
from .run import (
    _LEASED,
    _SUSPENDED,
    _utcnow,
)


class TakeoverReason(StrEnum):
    """抢到的是别人留下的 run，跑之前要先收拾现场。"""

    #: 上一个持有者停机前主动交出（owner 已清空）
    HANDOFF = "handoff"
    #: 持有者的租约过期了，多半是进程死了
    EXPIRED = "expired"


@dataclass(frozen=True)
class Taken:
    """抢到了。takeover 为 None = 新 run 或续跑（排队中的）。"""

    takeover: TakeoverReason | None


@dataclass(frozen=True)
class Candidate:
    """兜底任务的一条候选。id 是翻页游标：按 id 往后翻，一轮里每条只看一次。"""

    id: int
    run_uuid: UUID


def _orphaned(now: datetime) -> ColumnElement[bool]:
    """在途、但没人拿着：持有者的租约过期了，或停机前交出来了（交接让租约立即到期）。

    ★ 挂起时清了租约（lease_until 为空）：它由续跑放回排队，不是接管。不能用 waiting_on
      判断 —— 续跑段带着上一次挂起的 waiting_on 在跑，它崩了也要能被接管。
    """
    return and_(Run.status.in_(_LEASED), Run.lease_until.isnot(None), Run.lease_until < now)


class RunQueue:
    """抢锁、续租、交接、放回排队、兜底候选。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def take(self, run_id: UUID, *, owner: str, lease_s: float) -> Taken | None:
        """按 run 号抢锁：排队中的、租约过期的、交接了没人接的才抢得到。没抢到返回 None。

        ★ SELECT … FOR UPDATE SKIP LOCKED 再 UPDATE，同一事务：几个实例同时抢同一个 run，
          只有一个拿到行锁，其余的直接拿到空结果，不排队等。不写成 UPDATE … RETURNING：
          要知道抢之前的状态（判是不是接管），而 MySQL 没有 RETURNING，两种方言同一份代码。
        ★ 时间用本进程的 UTC 时钟：K8s 节点有 NTP，秒级偏差相对 30s 的租约可以忽略。
        """
        now = _utcnow()
        stmt = (
            select(Run.status, Run.owner)
            .where(Run.run_uuid == run_id, or_(Run.status == "queued", _orphaned(now)))
            .with_for_update(skip_locked=True)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        status, previous = row
        takeover = None
        if status != "queued":
            takeover = TakeoverReason.HANDOFF if previous is None else TakeoverReason.EXPIRED
        values: dict[str, Any] = {
            "status": "running",
            "owner": owner,
            "lease_until": now + timedelta(seconds=lease_s),
            "queued_at": None,
            "claimed_at": now,
            "started_at": func.coalesce(Run.started_at, now),
        }
        if takeover is not None:
            values["takeovers"] = Run.takeovers + 1
        await self._session.execute(update(Run).where(Run.run_uuid == run_id).values(**values))
        return Taken(takeover=takeover)

    async def renew(self, run_ids: list[UUID], *, owner: str, lease_s: float) -> set[UUID]:
        """一条 SQL 续本实例持有的全部 run。返回仍由本实例持有的那些。

        ★ 不在返回集合里的 = 已被别人抢走（或已不在租约态），调用方必须立即停止它的
          本地执行，否则就是「假死后复活」的双写。
        """
        if not run_ids:
            return set()
        held = (Run.run_uuid.in_(run_ids), Run.owner == owner, Run.status.in_(_LEASED))
        await self._session.execute(
            update(Run).where(*held).values(lease_until=_utcnow() + timedelta(seconds=lease_s))
        )
        return set((await self._session.execute(select(Run.run_uuid).where(*held))).scalars())

    async def release(self, run_id: UUID, *, owner: str) -> bool:
        """停机交接：清掉 owner、租约立即到期，别的实例的兜底任务下一轮就抢走。

        ★ 状态不动（仍是 running）：对用户来说这一轮没有停。
        """
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, Run.owner == owner, Run.status.in_(_LEASED))
            .values(owner=None, lease_until=_utcnow())
        )
        return bool(result.rowcount)

    async def requeue(self, run_id: UUID) -> bool:
        """把挂起的 run 放回排队（CAS：suspended → queued）。放回去了返回 True。

        放回去之后由触发续跑的那一方就地抢锁（审批决定的请求、最后结束的子 run 所在的实例）；
        没抢到的留在排队里，5 秒后成为兜底候选。

        ★ 必须是 CAS，不能先查后改。一轮里派出去的几个子 run 可能同时结束，每一个都会来
          续父 run —— 先查后改的话它们全都看到 suspended，同一个 run 会被放回好几次、
          抢到好几次。WHERE 里那个 status 才是真正的互斥，rowcount 是它的回执。
        """
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, Run.status.in_(_SUSPENDED))
            # ★ waiting_on **不清**：续跑段要把它和消息一起交回图，而这一段可能在任意
            #   实例上跑，只能从库里读（SegmentLoader.resume_state）。下一次挂起时覆盖，
            #   run 结束时清（finish）。读它的地方都按状态过滤（_wait_reasons）。
            .values(status="queued", queued_at=_utcnow())
        )
        return bool(result.rowcount)

    async def candidates(
        self, *, after_id: int, limit: int, queued_for_s: float
    ) -> list[Candidate]:
        """给兜底任务的候选：排队超过 queued_for_s 的、租约过期的、交接了没人接的。按 id 往后翻。

        ★ 不碰刚排队的：请求那条路从建 run 到本实例抢锁只有几毫秒，排队超过几秒还没人接，
          才说明接请求的实例确实没接下（满了）。queued_at 为空的是迁移前的旧数据，早就过了。
        """
        now = _utcnow()
        waited = or_(Run.queued_at.is_(None), Run.queued_at < now - timedelta(seconds=queued_for_s))
        stmt = (
            select(Run.id, Run.run_uuid)
            .where(
                Run.id > after_id,
                or_(and_(Run.status == "queued", waited), _orphaned(now)),
            )
            .order_by(Run.id)
            .limit(limit)
        )
        return [Candidate(id=i, run_uuid=u) for i, u in (await self._session.execute(stmt)).all()]
