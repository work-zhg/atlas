from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_agent.domain.events import EventType
from atlas_agent.domain.messages import KIND_CHAT

from ..db.models import Message, Run
from ..db.session import fetch_one

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

#: 持有租约（有执行器在跑）的状态（横向扩展设计 §4）。
#:
#: ★ awaiting_approval 也在其中：acp 的在线等审批 run 还在跑（bridge 等着权限响应），
#:   持有者死了要被接管。native 的审批挂起同一个状态值，但它挂起时清掉了租约
#:   （owner、lease_until 为空），不会被续租也不会被当成过期。
_LEASED = ("running", "awaiting_approval")


class LeaseLost(RuntimeError):
    """本实例已不再持有这个 run 的租约（被别的实例接管了）。

    ★ 抛出它的写入整个回滚：假死后复活的实例与接管者不能双写同一个 run（§13）。
    """

    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"run {run_id} 的租约已不在本实例")
        self.run_id = run_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _held_by(owner: str | None) -> tuple[Any, ...]:
    """围栏条件：owner 为 None 时不加任何条件。"""
    if owner is None:
        return ()
    return (Run.owner == owner, Run.status.in_(_LEASED))


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


def _suspended_status(waiting_on: dict[str, dict[str, str]] | None) -> str:
    """挂起落哪个状态值 —— 等人点头的那些要能被 UI 认出来。"""
    for wait in (waiting_on or {}).values():
        if (wait or {}).get("reason") == "approval":
            return "awaiting_approval"
    return "suspended"


def _wait_reasons(status: str, waiting_on: Any) -> set[str]:
    """挂起中的 run 在等哪几类东西。

    ★ 只认挂起态：waiting_on 留到 run 结束才清（续跑段要读它），续跑中的 run 带着
      上一次挂起的旧值 —— 不按状态过滤的话，一个正在跑的 run 会显示「需要我处理」。
    ★ 只认 dict：数组是旧格式的遗留数据，当成没有等待项。
    """
    if status not in _SUSPENDED or not isinstance(waiting_on, dict):
        return set()
    return {str(w.get("reason")) for w in waiting_on.values() if isinstance(w, dict)}


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
        return await fetch_one(self._session, Run, Run.run_uuid == run_id)

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
        stmt = select(Message).where(Message.thread_uuid == thread_id)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars())
        return list(reversed(rows))

    async def user_inputs(self, thread_id: UUID, *, limit: int = 100) -> list[Message]:
        """会话里用户说的话（不含工具结果），按时间正序，最多取最近 limit 条。

        自动模式的判断器用（kernel/middleware/auto_judge.py）。★ 不看 summary_upto：
        压缩过的早期消息也要 —— 判断器要知道用户最初要求的是什么。
        """
        stmt = (
            select(Message)
            .where(
                Message.thread_uuid == thread_id,
                Message.role == "user",
                Message.kind == KIND_CHAT,
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        return list(reversed(rows))

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
            select(Run.run_uuid)
            .where(Run.thread_uuid == thread_id, Run.status.in_(_UNFINISHED))
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
            select(Run.thread_uuid, func.max(Run.created_at).label("at"))
            .where(Run.thread_uuid.in_(thread_ids))
            .group_by(Run.thread_uuid)
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(Run.run_uuid, Run.thread_uuid, Run.status, Run.waiting_on).join(
                    latest,
                    (Run.thread_uuid == latest.c.thread_uuid) & (Run.created_at == latest.c.at),
                )
            )
        ).all()
        out: dict[UUID, dict[str, Any]] = {}
        for run_id, thread_id, status, waiting_on in rows:
            reasons = sorted(_wait_reasons(status, waiting_on))
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
                    select(Run.parent_run_uuid, Run.status, Run.waiting_on).where(
                        Run.parent_run_uuid.in_(list(parents)), Run.status.in_(_UNFINISHED)
                    )
                )
            ).all()
            for parent_id, status, waiting_on in children:
                if status == "awaiting_approval" or "approval" in _wait_reasons(status, waiting_on):
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
                Message.thread_uuid == thread_id,
                Message.role == "assistant",
                Message.kind == KIND_CHAT,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def run_output(self, run_id: UUID) -> tuple[str, int]:
        """一个 run 的 (最后一段非空文本, 工具调用次数) —— 委派结果整形用。

        ★ 按 **run** 取，不按会话取。持久子会话里有之前每次委派的消息：
          这次子 run 若没留下文本（或中途失败），按会话取「最后一条」会把
          上一次委派的旧结论当成这次的答复交回去。
        ★ 文本取「最后一条有文本的」，不是「最后一条」：一段在工具调用途中
          结束的 run，最后一条 assistant 只有 tool_use。
        """
        stmt = (
            select(Message.content)
            .where(
                Message.run_uuid == run_id,
                Message.role == "assistant",
                Message.kind == KIND_CHAT,
            )
            .order_by(Message.created_at, Message.id)
        )
        last_text, tool_calls = "", 0
        for content in (await self._session.execute(stmt)).scalars():
            blocks = content if isinstance(content, list) else []
            tool_calls += sum(
                1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
            )
            text = "".join(
                str(b.get("text") or "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if text:
                last_text = text
        return last_text, tool_calls

    async def count_active_subruns(self) -> int:
        """全进程正在执行的子 run 数 —— 准入控制的第一道闸。

        ★ 必须是全局的：acp 子智能体各吃一个 Pod，按 run 各自限流的话
          10 个并发会话每个委派 2 个 = 20 个 Pod。
        """
        stmt = (
            select(func.count())
            .select_from(Run)
            .where(Run.parent_run_uuid.isnot(None), Run.status.in_(_RUNNING))
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_subruns_in_tree(self, root_run_id: UUID) -> int:
        """这一轮（以 root_run_id 为根的整棵 run 树）累计派出过几个 SubAgent —— 准入控制的第二道闸。

        ★ 按整棵树数，不按直接父 run 数：SubAgent 可以继续派 SubAgent、层数不设上限，
          按父 run 数的话一条「A 派 A 再派 A……」的链每层都只派一个，永远撞不上限；
          而挂起中的子 run 又不算进全局并发（count_active_subruns）—— 递归会一直跑下去。
        ★ 也防另一种绕过：长 run 在每个规划点派一批「合法尺寸」的 SubAgent，
          累计起来远超并发限制。查 run 表即可，不用单独的账本。
        """
        stmt = select(func.count()).select_from(Run).where(Run.root_run_uuid == root_run_id)
        return int((await self._session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------ 写

    async def create(
        self,
        *,
        thread_id: UUID,
        agent_version_id: UUID,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
        root_run_id: UUID | None = None,
        depth: int = 0,
        created_by: UUID | None = None,
        auth_uid: UUID | None = None,
        auth_subject: str | None = None,
        approval_mode: str | None = None,
    ) -> Run:
        """run_id 可由调用方预生成：会话串行锁的 owner 值是 run_id，而锁必须
        先于 run 行拿到（拿不到就不该插行）—— 见 RunService.create。

        parent_run_id 非空即「这是 SubAgent 的子 run」；root_run_id / depth 是它在
        run 树里的位置（SubAgentService 按父 run 推出来）。

        approval_mode：根 run 取自会话 / 请求；子 run 是派活那一刻父 run 的模式。
        """
        run = Run(
            thread_uuid=thread_id,
            agent_version_uuid=agent_version_id,
            parent_run_uuid=parent_run_id,
            root_run_uuid=root_run_id,
            depth=depth,
            created_by_uuid=created_by,
            auth_uid=auth_uid or created_by,
            auth_subject=auth_subject,
            status="queued",
            approval_mode=approval_mode,
        )
        if run_id is not None:
            run.run_uuid = run_id
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
            .where(Run.run_uuid == run_id, Run.status.notin_(list(_TERMINAL)))
            .values(status=status)
        )

    # ------------------------------------------------------------------ 租约











