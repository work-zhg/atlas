from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_server.domain.events import EventType, TraceEvent
from atlas_server.domain.messages import KIND_CHAT

from ..db.models import AgentVersion, Message, Run, RunEvent, Thread

_TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")

#: 「有进程在跑，或马上要跑」。
#:
#: ★ 刻意**不含**挂起态：count_active_subruns 的准入控制数的是「正在吃资源的子 run」。
_RUNNING = ("queued", "running")

#: 挂起：**没有进程**，但这个 run 没结束。
#:
#: ★ awaiting_approval 从 _RUNNING 搬到了这里（S7）。它的含义变了：原先是
#:   「有一个 asyncio.Task 正阻塞在 BLPOP 上」，所以进程死了它确实是孤儿；
#:   现在是「挂起中，等人点头」—— 没有进程是正常的，收它就是把等待中的审批
#:   全判死。
#:
#: ★ 为什么不与 suspended 合并成一个值：UI 要区分「在等机器」和「在等你」。
#:   前者用户只能干等，后者需要他行动（去点那个弹窗）。
SUSPENDED_STATUSES = ("suspended", "awaiting_approval")
_SUSPENDED = SUSPENDED_STATUSES


#: 「用户眼里这个 run 还没结束」。
_UNFINISHED = (*_RUNNING, *_SUSPENDED)

#: 不进归档表的事件类型。
#:
#: ★ delta 是 token 级的，一次回答几百到几千条。它们的价值是**实时**打字
#:   效果 —— 而归档的唯一用途是「超过 Redis TTL 之后回放」，那时用户看的是
#:   历史，正文从 message.completed 一次到位即可（前端读历史本来走 message
#:   表，不靠 delta）。
#:
#: ★ 不滤的代价是实打实的：一个长会话几十万行，加上子 run 的 delta 还要
#:   翻倍，而其中绝大多数永远不会被读到。
_ARCHIVED_EXCLUDED = frozenset({EventType.MESSAGE_DELTA, EventType.THINKING_DELTA})


def _suspended_status(waiting_on: list[dict[str, str]] | None) -> str:
    """挂起落哪个状态值 —— 等人点头的那些要能被 UI 认出来。"""
    for wait in waiting_on or []:
        if (wait or {}).get("reason") == "approval":
            return "awaiting_approval"
    return "suspended"


def _usage_increments(usage: dict[str, int]) -> dict[str, Any]:
    """用量列的**累加**表达式。

    ★ 累加而不是覆盖：一个 run 可以分多段执行（委派挂起后续跑），每段各报
      各的用量。覆盖的话账面上只剩最后一段 —— 用户看到「这轮花了 2k token」
      而实际烧了 200k，`max_total_tokens` 也随之形同虚设。

    ★ 对只跑一段的 run 完全等价：列的初值是 0，加一次就是覆盖。
    """
    return {
        "input_tokens": Run.input_tokens + usage.get("input_tokens", 0),
        "output_tokens": Run.output_tokens + usage.get("output_tokens", 0),
        "cache_read_tokens": Run.cache_read_tokens + usage.get("cache_read", 0),
        "thinking_tokens": Run.thinking_tokens + usage.get("thinking_tokens", 0),
        "total_tokens": Run.total_tokens + usage.get("total_tokens", 0),
    }


class RunRepository:
    """唯一接触 run / run_event 表的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ 读

    async def get(self, run_id: UUID) -> Run | None:
        return await self._session.get(Run, run_id)

    async def load_for_execution(
        self, run_id: UUID
    ) -> tuple[Run, Thread, AgentVersion] | None:
        """取执行一次 run 所需的三行。

        试跑移除后 thread_id / agent_version_id 均非空（迁移 0006），
        内连接即可 —— 查不到就是 run 真的不存在。
        """
        stmt = (
            select(Run, Thread, AgentVersion)
            .join(AgentVersion, AgentVersion.id == Run.agent_version_id)
            .join(Thread, Thread.id == Run.thread_id)
            .where(Run.id == run_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return (row[0], row[1], row[2]) if row else None

    async def history(
        self,
        thread_id: UUID,
        *,
        after: datetime | None = None,
        limit: int = 2_000,
    ) -> list[Message]:
        """按时间正序取对话历史。

        `after` 是摘要覆盖的边界（thread.summary_upto）：早于它的消息已经
        被压进摘要，不再重复送给模型（§7.4）。

        ★ limit 从 100 提到 2000 并**只作为安全网**。原先的 100 是一道
          无条件硬截断，且发生在压缩之前 —— 超过 100 条的会话，更早的消息
          直接消失，既不进模型也不产生摘要，用户感受是「它忘了」而不是
          「已压缩」。收缩上下文的职责现在唯一地归压缩。
        """
        stmt = select(Message).where(Message.thread_id == thread_id)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars())
        return list(reversed(rows))

    async def archived_events(self, run_id: UUID, *, after_seq: int) -> list[RunEvent]:
        stmt = (
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def archived_run_trace(self, run_id: UUID, *, limit: int = 5_000) -> list[RunEvent]:
        """一轮的完整归档轨迹：这个 run 自己的事件 + 它委派出去的子 run 的事件。

        ★ 子 run 靠 parent_run_id 找。委派深度结构性封顶 1，一层子查询就够。
        ★ 按 thread_seq 排：父子交错发生，thread_seq 才是它们在会话流上的真实顺序。
        ★ 归档被清理后这里返回空 —— 前端据此退回只显示正文。
        """
        children = select(Run.id).where(Run.parent_run_id == run_id)
        stmt = (
            select(RunEvent)
            .where((RunEvent.run_id == run_id) | RunEvent.run_id.in_(children))
            .order_by(RunEvent.thread_seq)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def archived_thread_events(
        self, thread_id: UUID, *, after_seq: int, limit: int = 5_000
    ) -> list[RunEvent]:
        """一条会话流上 thread_seq 之后的归档事件（跨 run，含子 run）。

        ★ 为什么会话流也要读归档：Redis 的流有 maxlen 裁剪，而会话流永不结束
          —— 活跃久了早期事件只在 Postgres 里。所以回放是「先归档、再 Redis」
          接力，两段按 thread_seq 接上（见 services/run.py::stream_thread）。

        ★ limit 是安全网而不是分页：正常路径由调用方先算出一个窗口下界
          （thread_seq_window_floor），不会一次要几千条。
        """
        stmt = (
            select(RunEvent)
            .where(RunEvent.thread_id == thread_id, RunEvent.thread_seq > after_seq)
            .order_by(RunEvent.thread_seq)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def thread_seq_window_floor(self, thread_id: UUID, *, keep: int) -> int:
        """「只回放最近 keep 条」对应的游标下界。

        ★ 为什么必须有这个窗口。会话流的订阅单位是 thread，而一条会话可以有
          几百轮 —— 首连若从 thread_seq=0 开始回放，打开一个老会话就是往前端
          灌几万个事件，页面直接卡死。这是 thread 流相对 run 流**新增**的风险
          （run 流天然只有一轮的量）。

        ★ 返回 0 = 这条会话的事件不足 keep 条，全给。
        """
        inner = (
            select(RunEvent.thread_seq)
            .where(RunEvent.thread_id == thread_id)
            .order_by(RunEvent.thread_seq.desc())
            .limit(keep)
            .subquery()
        )
        floor = (await self._session.execute(select(func.min(inner.c.thread_seq)))).scalar()
        # 下界是「最早那条的前一个」—— after_seq 语义是**严格大于**
        return max(int(floor) - 1, 0) if floor else 0

    async def suspended_runs(self) -> list[UUID]:
        """**真正挂起**的 run（含等审批的）。周期扫描用来补唤醒。

        ★ 为什么需要补：正常路径是「等的东西到了就主动唤醒」—— 子 run 收尾时、
          HTTP 决策提交时。但那一刻进程可能正好在重启，唤醒信号就这么没了，
          run 会永远挂着。这个扫描是它唯一的出路。

        ★ `waiting_on IS NOT NULL` 这一条不是优化，是**正确性**。
          `awaiting_approval` 这个状态值在两条路径上含义不同：

            native   图已跳出、进程已结束 —— 真挂起，waiting_on 有值
            acp      CLI 在 Pod 里发起的同步 RPC，**run 还在跑** —— bridge 正
                     等着响应，所以只能在线等（acp/runtime.py::_await_decision），
                     这期间 gate 也会把 run 标成 awaiting_approval，但它没有
                     waiting_on

          不加这一条的话，扫描会把一个**正在跑**的 acp 子 run 当成可续跑，
          submit 第二次 —— 第二条上游连接 连上同一个 Pod，bridge 按
          CLOSE_SUPERSEDED(4409) 关掉第一个，整轮以 runtime_crashed 失败。
          真机验证时撞到过，症状是「审批批准了，子 run 却崩了」。
        """
        stmt = select(Run.id).where(
            Run.status.in_(_SUSPENDED), Run.waiting_on.isnot(None)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def has_unfinished_children(self, run_id: UUID) -> bool:
        """这个 run 还有没跑完的子 run 吗 —— 续跑的 join barrier。

        ★ 判据直接查 run 表，不需要单独的委派账本：`parent_run_id` 本来就
          记着这层关系（迁移 0007），`ix_run_parent` 本来就在。已经终态的
          子 run（包括前几段里完成的那些）天然不计入。
        """
        stmt = (
            select(func.count())
            .select_from(Run)
            .where(Run.parent_run_id == run_id, Run.status.in_(_UNFINISHED))
        )
        return int((await self._session.execute(stmt)).scalar_one()) > 0

    async def unfinished_children_of(self, run_id: UUID) -> list[UUID]:
        """还在跑的子 run —— 父被取消时要级联取消它们。

        ★ 不级联的后果很贵：父 run 没了，`_await_result` 的轮询循环随之消失，
          再没有任何人去取消子 run。它会一直跑到自己的超时，而 acp 的子 run
          整段时间都占着一个 Pod。
        """
        stmt = select(Run.id).where(
            Run.parent_run_id == run_id, Run.status.in_(_UNFINISHED)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def active_run_of(self, thread_id: UUID) -> UUID | None:
        """该会话当前**还没跑完**的 run —— 刷新页面后恢复事件流要用它。

        ★ 没有它的话，刷新 = 白屏：前端的 activeRunId 只在「发消息成功」时
          赋值，重新挂载后是 undefined，于是不订阅任何流；而助手消息要到
          message.completed 才落库，长 run 期间 message 表里只有用户那条。
          用户看到的就是「回答到一半没了，刷新后什么都没有」。

        ★ 口径是 _UNFINISHED 而不是 _RUNNING：等审批的 run 在用户眼里正是
          「还在跑」，恢复出来才能看见弹窗；等子智能体的（suspended）同理，
          他正盯着那个「委派中」的卡片。两者都没有进程在跑，但都没结束。
        """
        stmt = (
            select(Run.id)
            .where(Run.thread_id == thread_id, Run.status.in_(_UNFINISHED))
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def latest_runs_of(self, thread_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
        """一页会话各自**最近一个** run 的状态 —— 会话列表的状态徽标用。

        两次查询，与页大小无关（不 N+1）：
          1. 每个会话最近一个 run（按 created_at）
          2. 这些 run 的子 run 里，有没有在等审批的

        ★ 第 2 步不能省：委派出去的子智能体请求审批时，父 run 显示的是「等待子
          智能体」，但真正卡住的是用户 —— 「需要我处理」必须把它算进去。
        """
        if not thread_ids:
            return {}
        latest = (
            select(Run.thread_id, func.max(Run.created_at).label("at"))
            .where(Run.thread_id.in_(thread_ids))
            .group_by(Run.thread_id)
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(Run.id, Run.thread_id, Run.status, Run.waiting_on).join(
                    latest, (Run.thread_id == latest.c.thread_id) & (Run.created_at == latest.c.at)
                )
            )
        ).all()
        out: dict[UUID, dict[str, Any]] = {}
        for run_id, thread_id, status, waiting_on in rows:
            reasons = sorted(
                {str(w.get("reason")) for w in (waiting_on or []) if isinstance(w, dict)}
            )
            out[thread_id] = {
                "id": run_id,
                "status": status,
                "waiting": reasons,
                "needs_approval": status == "awaiting_approval" or "approval" in reasons,
            }
        parents = {v["id"]: tid for tid, v in out.items() if v["status"] in _SUSPENDED}
        if parents:
            children = (
                await self._session.execute(
                    select(Run.parent_run_id, Run.status, Run.waiting_on).where(
                        Run.parent_run_id.in_(list(parents)), Run.status.in_(_UNFINISHED)
                    )
                )
            ).all()
            for parent_id, status, waiting_on in children:
                waits = {str(w.get("reason")) for w in (waiting_on or []) if isinstance(w, dict)}
                if status == "awaiting_approval" or "approval" in waits:
                    out[parents[parent_id]]["needs_approval"] = True
        return out

    async def last_assistant_content(self, thread_id: UUID) -> list | None:
        """子会话最后一条 assistant 消息的内容 —— 委派的返回值。

        ★ 取自 message 表而不是事件流：message 是事实源，而事件在 Redis 里
          有 TTL。子 run 跑完之后父才来读，中间可能隔着审批等待。
        """
        stmt = (
            select(Message.content)
            .where(
                Message.thread_id == thread_id,
                Message.role == "assistant",
                Message.kind == KIND_CHAT,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def count_active_subruns(self) -> int:
        """全进程正在执行的子 run 数 —— 准入控制的第一道闸。

        ★ 必须是全局的：acp 子智能体各吃一个 Pod，按 run 各自限流的话
          10 个并发会话每个委派 2 个 = 20 个 Pod。
        """
        stmt = (
            select(func.count())
            .select_from(Run)
            .where(Run.parent_run_id.isnot(None), Run.status.in_(_RUNNING))
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_subruns_of(self, run_id: UUID) -> int:
        """某个父 run 累计发起过几次委派 —— 准入控制的第二道闸。

        防的是一种绕过：长 run 在每个规划点发起一批「合法尺寸」的委派，
        累计起来远超并发限制。查 run 表即可，不用单独的委派账本。
        """
        stmt = select(func.count()).select_from(Run).where(Run.parent_run_id == run_id)
        return int((await self._session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------ 写

    async def create(
        self,
        *,
        thread_id: UUID,
        agent_version_id: UUID,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
    ) -> Run:
        """run_id 可由调用方预生成：会话串行锁的 owner 值是 run_id，而锁必须
        先于 run 行拿到（拿不到就不该插行）—— 见 RunService.create。

        parent_run_id 非空即「这是一次委派产生的子 run」。
        """
        run = Run(
            thread_id=thread_id,
            agent_version_id=agent_version_id,
            parent_run_id=parent_run_id,
            status="queued",
        )
        if run_id is not None:
            run.id = run_id
        self._session.add(run)
        await self._session.flush()
        await self._session.refresh(run)
        return run

    async def set_status(self, run_id: UUID, status: str) -> None:
        """仅用于运行中的状态切换（running ⇄ awaiting_approval）。

        ★ 不碰终止态：run 一旦 succeeded/failed/cancelled，事件集就是不可变的
          （§10.2 的回放依赖这一点），把它改回 running 会让已关闭的 SSE
          再也无法正确回放。
        """
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.notin_(list(_TERMINAL)))
            .values(status=status)
        )

    async def mark_running(self, run_id: UUID) -> None:
        """★ started_at 只在第一次设。

        一个 run 可以分多段执行，每段都会走到这里。每次都重设的话，
        「这一轮跑了多久」会变成「最后一段跑了多久」—— 而委派挂起的那一
        小时恰恰是最该被看见的部分。COALESCE 让它保持第一段的时间。
        """
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status="running",
                started_at=func.coalesce(Run.started_at, datetime.now(UTC)),
            )
        )

    async def finish(
        self,
        run_id: UUID,
        *,
        status: str,
        last_seq: int,
        usage: dict[str, int],
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status,
                last_seq=last_seq,
                error_kind=error_kind,
                error_message=error_message,
                **_usage_increments(usage),
                finished_at=datetime.now(UTC),
            )
        )

    async def suspend(
        self,
        run_id: UUID,
        *,
        last_seq: int,
        usage: dict[str, int],
        waiting_on: list[dict[str, str]] | None = None,
    ) -> None:
        """一段跑完了，但这个 run 还没结束 —— 等子 run 回来再续。

        ★ 用量与 seq 必须当场落下。下一段要从 `last_seq` 接着数事件序号
          （契约规则 2 的「无空洞」跨段成立），也要从 `total_tokens` 接着
          算 token 上限 —— 每段各算各的话，一个 run 分三段就能烧掉三倍的
          预算，而「刹车」在账面上看起来一直没踩。

        ★ 不写 finished_at：它没结束。

        ★ waiting_on 记「在等什么」（含 reason）。续跑的 barrier 按它分派 ——
          委派等子 run 终态，审批等 approval 被决策。只记 token 的话审批挂起
          会被当成委派，barrier 立刻满足，于是死循环（迁移 0012）。
        """
        await self._session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                # ★ 等人点头落 awaiting_approval，等机器落 suspended。
                #   两者都是挂起（_SUSPENDED），分开只为让 UI 能区分
                #   「你需要做点什么」和「干等就行」。
                status=_suspended_status(waiting_on),
                last_seq=last_seq,
                waiting_on=waiting_on or None,
                **_usage_increments(usage),
            )
        )

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
            .where(Run.id == run_id, Run.status.in_(_SUSPENDED))
            # waiting_on 一并清掉：它描述的是**这一次**挂起在等什么，续跑之后
            # 就过期了。留着的话下一次挂起如果写入失败，barrier 会拿旧值判断。
            .values(status="queued", waiting_on=None)
        )
        return bool(result.rowcount)

    async def archive_events(self, events: list[TraceEvent], *, thread_id: UUID) -> None:
        """每段结束时批量落库 —— Redis 是实时通道，这里是历史（文档 §10.2）。

        thread_id 是**事件流归属的会话**（根会话，Thread.stream_thread_id），
        与 run.thread_id 在子 run 上是不同的值。

        ★ delta 类事件不归档（_ARCHIVED_EXCLUDED）。它们的价值是实时打字
          效果；回放历史时正文来自 message.completed 一次到位，而前端读
          历史本来就走 message 表。不滤的话一个长会话能到几十万行，其中
          绝大多数永远不会被读到。

        ★ 只落已盖过会话序号的事件。thread_seq 是主键的一半，为 0 说明它
          没经过 publish —— 那是个 bug，宁可少归档一条也不能让整批 INSERT
          撞主键（那会让整段的收尾失败，run 卡在 running）。
        """
        rows = [
            {
                "thread_id": thread_id,
                "thread_seq": e.thread_seq,
                "run_id": e.run_id,
                "seq": e.seq,
                "ts": e.ts,
                "type": e.type.value,
                "depth": e.depth,
                "data": e.data,
            }
            for e in events
            if e.type not in _ARCHIVED_EXCLUDED and e.thread_seq > 0
        ]
        if not rows:
            return
        await self._session.execute(insert(RunEvent), rows)

    async def thread_seq_floor(self, thread_id: UUID) -> int:
        """这条会话已归档的最大 thread_seq —— Redis 序号丢失时的恢复水位。

        ★ 每段执行开始时查一次，不是每个事件查一次。它只在 Redis 的序号 key
          不存在时才真正被用到（见 EventRelay.next_thread_seq 的 Lua）。
        """
        stmt = select(func.max(RunEvent.thread_seq)).where(RunEvent.thread_id == thread_id)
        return int((await self._session.execute(stmt)).scalar() or 0)

    async def touch_thread(self, thread_id: UUID, *, latest_state: dict | None = None) -> None:
        values: dict = {
            "updated_at": func.now(),
            # ★ 只数对话轮，与 ThreadRepository.list_messages / count_messages
            #   同口径。工具结果也数进去的话，一轮调十次工具就凭空多二十条，
            #   而用户只发了一句话。
            "message_count": (
                select(func.count())
                .select_from(Message)
                .where(Message.thread_id == thread_id, Message.kind == KIND_CHAT)
                .scalar_subquery()
            ),
        }
        if latest_state is not None:
            values["latest_state"] = latest_state
        await self._session.execute(update(Thread).where(Thread.id == thread_id).values(**values))
