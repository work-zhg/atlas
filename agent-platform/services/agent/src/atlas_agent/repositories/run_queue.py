"""run 表上与调度有关的读写（Run 执行器 · 调度与租约，code-executor-scheduling.html）。

领取（CAS + 租约）、续租、交接、把挂起的 run 放回队列。哪个实例来跑一个 run 只看这里。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Run
from .run import (
    _LEASED,
    _SUSPENDED,
    _utcnow,
)


class RunQueue:
    """领取、续租、交接、放回队列。领取用 SKIP LOCKED，续租一条 SQL 续本实例的全部 run。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_for_resume(self, run_id: UUID) -> bool:
        """把一个 suspended 的 run 抢过来续跑。抢到返回 True。

        ★ 必须是 CAS，不能先查后改。一轮里派出去的几个子 run 可能同时
          结束，每一个都会来唤醒父 run —— 先查后改的话它们全都看到
          `suspended` 并各自起一个续跑任务，于是同一个 run 有好几个进程
          在跑，事件序号与落库互相踩。
          WHERE 里那个 status 才是真正的互斥，rowcount 是它的回执。
        """
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, Run.status.in_(_SUSPENDED))
            # ★ waiting_on **不清**：续跑段要把它和消息一起交回图，而这一段可能由任意
            #   实例领取，只能从库里读（executor._resume_state）。下一次挂起时覆盖，
            #   run 结束时清（finish）。读它的地方都按状态过滤（_wait_reasons）。
            .values(status="queued")
        )
        return bool(result.rowcount)

    async def claim_next(self, *, owner: str, lease_s: float) -> tuple[UUID, str | None] | None:
        """领取一个 run：排队中的，或租约已过期的在途 run（接管）。抢到返回 (run_id, 接管原因)。

        接管原因：None = 正常领取（新 run 或挂起后续跑）；"handoff" = 上一个持有者停机前
        主动交接（owner 已清空）；"expired" = 持有者租约过期（多半是进程死了）。

        ★ SELECT … FOR UPDATE SKIP LOCKED 再 UPDATE，同一事务：多个执行器并发领取互不
          阻塞、也不会领到同一行。不写成 UPDATE … WHERE id = (子查询)：MySQL 不允许
          UPDATE 的子查询读同一张表，这样两种方言同一份代码。
        ★ 时间用本进程的 UTC 时钟：K8s 节点有 NTP，秒级偏差相对 30s 的租约可以忽略。
        """
        now = _utcnow()
        expired = and_(
            Run.status.in_(_LEASED),
            # native 的审批挂起没有进程在跑：挂起时清了租约（lease_until 为空），它由续跑
            # 路径（claim_for_resume）唤醒，不是接管。★ 不能再用 waiting_on 判断 —— 续跑段
            # 带着上一次挂起的 waiting_on 在跑，它崩了也要能被接管。
            Run.lease_until.isnot(None),
            Run.lease_until < now,
        )
        stmt = (
            select(Run.run_uuid, Run.status, Run.owner)
            .where(or_(Run.status == "queued", expired))
            .order_by(Run.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        run_id, status, previous = row
        reason = None if status == "queued" else ("handoff" if previous is None else "expired")
        values: dict[str, Any] = {
            "status": "running",
            "owner": owner,
            "lease_until": now + timedelta(seconds=lease_s),
            "claimed_at": now,
            "started_at": func.coalesce(Run.started_at, now),
        }
        if reason is not None:
            values["takeovers"] = Run.takeovers + 1
        await self._session.execute(update(Run).where(Run.run_uuid == run_id).values(**values))
        return run_id, reason

    async def renew_leases(self, run_ids: list[UUID], *, owner: str, lease_s: float) -> set[UUID]:
        """一条 SQL 续本实例持有的全部 run。返回仍由本实例持有的那些。

        ★ 不在返回集合里的 = 已被别人接管（或已不在租约态），调用方必须立即停止它的
          本地执行，否则就是「假死后复活」的双写。
        """
        if not run_ids:
            return set()
        held = (Run.run_uuid.in_(run_ids), Run.owner == owner, Run.status.in_(_LEASED))
        await self._session.execute(
            update(Run).where(*held).values(lease_until=_utcnow() + timedelta(seconds=lease_s))
        )
        return set((await self._session.execute(select(Run.run_uuid).where(*held))).scalars())

    async def release_lease(self, run_id: UUID, *, owner: str) -> bool:
        """主动交接（§6）：清掉 owner、租约立即到期，下一次领取扫描就会把它领走。

        ★ 状态不动（仍是 running）：对用户来说这一轮没有停。
        """
        result = await self._session.execute(
            update(Run)
            .where(Run.run_uuid == run_id, Run.owner == owner, Run.status.in_(_LEASED))
            .values(owner=None, lease_until=_utcnow())
        )
        return bool(result.rowcount)
