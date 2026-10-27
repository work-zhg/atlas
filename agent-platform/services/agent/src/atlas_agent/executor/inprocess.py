"""RunExecutor：领取 + 租约 + 检查点（横向扩展设计 §4–§6）—— 只负责执行循环。

「一轮怎么跑」在 runtime.py（AgentRuntime）：本文件的前后两段——载入/标记
运行中，与收事件/落库/释放锁——与 agent 类型无关，acp 接进来时一行不改
（acp 详设 §02）。能力注入的组装在 assembly.py（HookAssembly）。

run 不再是「接到请求的那个进程的任务」，而是**数据库里可被任何实例领取、续跑的工作**：

    submit    → 只发唤醒（run 行在建的时候就是 queued）
    领取      → CAS + 租约（claim_next），本实例并发满了就不领（背压）
    续租      → 每 lease/3 一条 SQL 续本实例的全部 run；续不上 = 被接管了，立即停
    检查点    → 每个工具轮次把消息与事件落库（native），进程死了最多丢一轮
    接管      → 租约过期的 run 被别的实例领走：补归档、给悬空调用补「结果未知」、接着跑
    交接      → 停机时 native 走到安全点、acp 直接断开，清租约让别的实例立刻领走

★ 不设到点结束的超时：租约是「进程还活着」的心跳，不是工作时长上限。

后台任务**必须自建 session 与 Redis 连接** —— 创建它的那个 HTTP 请求早已结束，
其 session 也已关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.kernel.middleware.compaction import SUMMARY_PREFIX
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.events import EventFactory, EventType, TraceEvent
from atlas_agent.domain.messages import (
    KIND_CHAT,
    StoredMessage,
    Transcript,
    from_storage,
    interrupted_results,
)
from atlas_agent.domain.spec import AgentSpec, spec_for_subagent

from ..config import Settings
from ..db.models import AgentVersion, Message, Run, Thread
from ..instance import instance_id
from ..outbound import CallIdentity, use_identity
from ..redisx import make_redis
from ..repositories.approval import ApprovalRepository
from ..repositories.run import SUSPENDED_STATUSES, LeaseLost, RunRepository
from ..repositories.skill_usage import SkillUsageRepository
from ..repositories.thread import ThreadRepository
from ..schemas.agent import AgentSpecIn
from ..services.subagent import subagent_result
from ..stream.relay import RUN_WAKE_CHANNEL, EventRelay
from ..telemetry.run_trace import RunTrace
from .assembly import HookAssembly, ModelBuilder, PreparedRun, default_model_builder
from .runtime import NativeRuntime, select_runtime

if TYPE_CHECKING:
    from ..memory import MemoryRuntime

logger = logging.getLogger(__name__)

_STATUS_BY_TERMINAL = {
    EventType.RUN_FINISHED: "succeeded",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_CANCELLED: "cancelled",
}

#: 不算「正文还在流」的事件 —— 接管时算要丢弃的正文用
_DELTAS = frozenset({EventType.MESSAGE_DELTA, EventType.THINKING_DELTA})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _last_chat_input(rows: list[Message]) -> int | None:
    """最后一条 kind='chat' 的用户消息在 rows 里的下标。None = 一条都没有。"""
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].role == "user" and rows[index].kind == KIND_CHAT:
            return index
    return None


async def _save_approval_mode(
    session: AsyncSession, run_id: UUID, thread_id: UUID, mode: str
) -> None:
    """计划获批后的新模式。

    run 续跑、接管时从 run.approval_mode 读；会话记住它，作用于下一条消息。
    """
    await session.execute(update(Run).where(Run.id == run_id).values(approval_mode=mode))
    await session.execute(update(Thread).where(Thread.id == thread_id).values(approval_mode=mode))


def _spec_from_version(version: AgentVersion, *, slug: str, name: str) -> AgentSpec:
    return AgentSpecIn.model_validate(version.spec).to_engine(slug=slug, name=name)


def _today() -> date:
    return datetime.now(UTC).date()


def _discarded_chars(events: list[TraceEvent], depth: int) -> int:
    """接管时要丢弃的正文：最后一个非 delta 事件之后流出的那段（没落库，会重做）。"""
    chars = 0
    for event in events:
        if event.type not in _DELTAS:
            chars = 0
        elif event.type is EventType.MESSAGE_DELTA and event.depth == depth:
            chars += len(str(event.data.get("text") or ""))
    return chars


@dataclass
class _Slot:
    """本实例上一个 run 的执行槽。"""

    task: asyncio.Task[None] | None = None
    thread_id: UUID | None = None
    #: 收尾已开始落库：不再续租、续不上也不算「被接管」（终态/挂起会清掉租约）
    settled: bool = False
    #: 租约已不在本实例（被别的实例接管）：什么都不能再写，锁也不能放
    lost: bool = False
    #: 停机中：native 到安全点就交接
    drain: bool = False
    acp: bool = False


class InProcessExecutor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        model_builder: ModelBuilder | None = None,
        acp_runtime: Any = None,
        memory: MemoryRuntime | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        # 可注入：单测塞一个假模型，整条执行链路即可脱离网络运行
        self._build_model = model_builder or default_model_builder
        self._assembly = HookAssembly(
            sessionmaker,
            settings,
            self._build_model,
            # 委派要能起子 run —— 它走的就是本执行器的 submit，与用户发起的
            # run 完全同一条链路（设计 §06）。
            executor=self,
            # 常驻记忆与记忆工具（doc/memory-design.html §4–§5）
            memory=memory,
        )
        #: 进程级复用：assembly 持有实例态（沙箱/编码后端），每轮新建会把
        #: 那些状态打散。每轮变化的东西全在 run_turn 的参数里。
        self._native = NativeRuntime(self._assembly)
        #: acp 执行环境。None = 本部署没接 Pod —— kind="acp" 的 agent 会在
        #: select_runtime 里明确报错，而不是悄悄按 native 跑。
        self._acp = acp_runtime
        #: 租约的 owner。
        self._instance = instance_id()
        #: api 角色只入队、不执行（横向扩展设计 §3）
        self._executes = settings.server_role != "api"
        self._slots: dict[UUID, _Slot] = {}
        #: 领取突发（submit / 唤醒 / 扫描触发）的任务引用 —— asyncio 只持弱引用
        self._kicks: set[asyncio.Task[None]] = set()
        self._claim_lock = asyncio.Lock()
        #: 周期任务：补唤醒扫描、领取扫描、唤醒订阅
        self._loops: list[asyncio.Task[None]] = []
        #: 续租任务。有 run 在跑才需要，按需起
        self._renewer: asyncio.Task[None] | None = None
        self._draining = False
        self._drain_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ 协议

    @property
    def model_builder(self) -> Any:
        """与执行器同一个模型构造器（手动压缩等 API 侧的模型调用用它，测试可注入）。"""
        return self._build_model

    @property
    def draining(self) -> bool:
        return self._draining

    async def submit(self, run_id: UUID) -> None:
        """run 行已是 queued：广播唤醒，本实例也立刻试着领一次。

        ★ 不再「在接到请求的进程里起任务」：领到它的可能是任何一个实例（§4.2）。
          本实例若是执行器且有空位，通常就是它自己 —— 与原来的延迟相当。
        """
        await self._wake()
        self._kick()

    async def cancel(self, run_id: UUID) -> None:
        redis = self._redis()
        try:
            await EventRelay(redis).request_cancel(run_id)
        finally:
            await redis.aclose()

        # ★ 挂起中的 run **没有进程**会读到这个取消位。
        #
        #   别处的取消都是「设位，让正在跑的循环自己看见」——  runner 每步
        #   检查一次，到安全点产出 run.cancelled 然后走正常收尾。suspended
        #   没有那个循环：不当场收的话，这个 run 会一直躺着，会话也一直被
        #   它占着（用户连下一句都发不出），直到某个子 run 恰好跑完才被
        #   resume_if_ready 顺手发现。而子 run 可能要跑一小时。
        #
        #   ★ 不等子 run。用户要的是**现在**停下；子 run 已经被级联取消
        #     （RunService.cancel），它自己会收自己的尾。
        async with self._sessionmaker() as session:
            run = await RunRepository(session).get(run_id)
        if run is not None and run.status in SUSPENDED_STATUSES:
            await self._settle_cancelled(run_id)

    async def shutdown(self) -> None:
        """停机：先交接在跑的 run（§6），再停周期任务。"""
        await self.drain()
        for task in [*self._loops, *self._kicks]:
            task.cancel()
        for task in [*self._loops, *self._kicks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._loops.clear()
        self._kicks.clear()

    # ------------------------------------------------------------------ 周期任务

    def start(self) -> None:
        """起本实例的周期任务。api 角色什么都不起 —— 它不执行 run。

        补唤醒扫描   丢了唤醒信号的挂起 run 只有它能捞回来（CAS 幂等，每个执行器都扫）
        领取扫描     唤醒丢了也不丢 run；接管租约过期的 run 也靠它
        唤醒订阅     新 run 入队后秒级领取，不必等扫描
        """
        if self._loops or not self._executes:
            return
        self._loops = [
            asyncio.create_task(self._sweep_forever(), name="suspended-sweeper"),
            asyncio.create_task(self._claim_forever(), name="run-claimer"),
            asyncio.create_task(self._listen_wakes(), name="run-wake-listener"),
        ]

    async def _sweep_forever(self) -> None:
        """把丢了唤醒信号的 suspended run 捞回来。

        ★ 为什么不能只靠子 run 主动唤醒：那个唤醒发生在子 run 收尾的代码里，
          而进程可能正好在那一刻被 SIGKILL。信号没了之后**没有任何其它机制**
          会再碰这个父 run。这个扫描是它唯一的出路。

        ★ 幂等靠 claim_for_resume 的 CAS：扫描与主动唤醒撞上时只有一个能赢。
        """
        interval = self._settings.suspended_sweep_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep_suspended()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 扫描失败只是这一轮没捞到，下一轮还会来 —— 绝不能让它把
                # 循环带走，否则「唯一的出路」就没了。
                logger.warning("suspended 扫描失败", exc_info=True)

    async def sweep_suspended(self) -> int:
        """扫一遍挂起的 run，能续的续上。返回续了几个。

        ★ 不判超时：审批只由人决定，等多久都不替用户按拒绝处理。
        """
        async with self._sessionmaker() as session:
            candidates = await RunRepository(session).suspended_runs()
        resumed = 0
        for run_id in candidates:
            if await self.resume_if_ready(run_id):
                resumed += 1
        if resumed:
            logger.info("补唤醒了 %d 个等待中的 run", resumed)
        return resumed

    async def _claim_forever(self) -> None:
        while True:
            await self.claim_available()
            await asyncio.sleep(self._settings.run_claim_scan_s)

    async def _listen_wakes(self) -> None:
        """订阅唤醒广播。断了就退避重连 —— 它只是加速，断着也有扫描兜底。"""
        delay = 1.0
        while True:
            redis = self._redis()
            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(RUN_WAKE_CHANNEL)
                delay = 1.0
                async for message in pubsub.listen():
                    if message.get("type") == "message":
                        self._kick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("唤醒订阅断开，%.0fs 后重连", delay, exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    await redis.aclose()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _wake(self) -> None:
        redis = self._redis()
        try:
            await redis.publish(RUN_WAKE_CHANNEL, "1")
        except Exception:  # 唤醒丢了只是慢一点：扫描会领到它
            logger.warning("发布 run 唤醒失败", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    # ------------------------------------------------------------------ 领取

    def _kick(self) -> None:
        """在后台试着领取。★ 全新的 contextvars.Context，理由见 _spawn。"""
        if not self._executes or self._draining:
            return
        task = asyncio.create_task(
            self.claim_available(), name="run-claim", context=contextvars.Context()
        )
        self._kicks.add(task)
        task.add_done_callback(self._kicks.discard)

    async def claim_available(self) -> int:
        """领到本实例的并发上限为止。返回领了几个。

        ★ 串行领取（锁）：并发的几次 kick 各自领的话，会一起冲过并发上限。
        """
        claimed = 0
        async with self._claim_lock:
            while not self._draining and len(self._slots) < self._settings.max_concurrent_runs:
                try:
                    async with self._sessionmaker() as session:
                        got = await RunRepository(session).claim_next(
                            owner=self._instance, lease_s=self._settings.run_lease_s
                        )
                        await session.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("领取 run 失败", exc_info=True)
                    break
                if got is None:
                    break
                run_id, takeover = got
                if takeover is not None:
                    logger.warning("接管 run %s（%s）", run_id, takeover)
                self._spawn(run_id, takeover)
                claimed += 1
        return claimed

    def _spawn(self, run_id: UUID, takeover: str | None) -> None:
        # ★ 本实例上还有这个 run 的旧任务（例如事件循环卡过、租约过期后自己又领了回来）：
        #   旧的必须停，同一个 run 不能有两个本地执行。
        #   已在收尾的（settled）不算：挂起后续跑时，上一段的任务正是在收尾里唤醒了这一段
        old = self._slots.get(run_id)
        if old is not None and not old.settled and old.task and not old.task.done():
            old.lost = True
            old.task.cancel()

        slot = _Slot()
        # ★ 必须给一个**全新的** contextvars.Context。
        #
        #   委派让 submit 第一次可能在「另一个 run 的执行上下文里」被调用
        #   （父 run 的 task 工具起子 run）。asyncio.create_task 默认拷贝当前
        #   上下文，而 langgraph 的流式输出与 langsmith 的追踪都挂在 contextvar
        #   上 —— 继承过去的话子 run 的 token 会被写进**父 run 的**流通道，
        #   子 run 自己的 message.delta 一个都不产出，最终落库的 assistant
        #   消息是空的。症状极具迷惑性：子 run 状态 succeeded、模型确实被调用、
        #   只是内容凭空消失。
        slot.task = asyncio.create_task(
            self._guarded(run_id, slot, takeover),
            name=f"run:{run_id}",
            context=contextvars.Context(),
        )
        self._slots[run_id] = slot

        # ★ 只删自己。续跑时上一段的任务还没退出、新一段已经领到了同一个 run_id：
        #   回调若无条件 pop，删掉的就是新任务 —— 那是它唯一的强引用（asyncio 对任务
        #   只持弱引用），新任务会在运行中被回收（2026-10-13 智能体评测：多次审批的
        #   run 偶发卡死）。
        def _forget(_t: asyncio.Task[None]) -> None:
            if self._slots.get(run_id) is slot:
                self._slots.pop(run_id, None)

        slot.task.add_done_callback(_forget)
        if self._renewer is None or self._renewer.done():
            self._renewer = asyncio.create_task(self._renew_forever(), name="run-lease-renewer")

    # ------------------------------------------------------------------ 续租

    async def _renew_forever(self) -> None:
        """每 lease/3 续一次本实例的全部 run；没有 run 了就退出（下次领取时再起）。"""
        interval = max(self._settings.run_lease_s / 3, 0.2)
        while self._slots:
            await asyncio.sleep(interval)
            try:
                await self.renew_leases()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 数据库抖动：这一轮没续上，下一轮再来。连续失败到租约过期 = 被接管，
                # 那时下一次续租会发现并停掉本地执行
                logger.warning("run 租约续期失败", exc_info=True)

    async def renew_leases(self) -> None:
        live = {rid: s for rid, s in self._slots.items() if not s.settled and not s.lost}
        if not live:
            return
        async with self._sessionmaker() as session:
            held = await RunRepository(session).renew_leases(
                list(live), owner=self._instance, lease_s=self._settings.run_lease_s
            )
            await session.commit()
        redis = self._redis()
        try:
            relay = EventRelay(redis)
            for run_id, slot in live.items():
                if slot.settled:  # 续租期间刚好收尾了
                    continue
                if run_id not in held:
                    # ★ 被接管了：立即停止本地执行，防止「假死后复活」的双写（§4.3）
                    logger.warning("run %s 的租约已被别的实例接管，停止本地执行", run_id)
                    slot.lost = True
                    if slot.task is not None:
                        slot.task.cancel()
                elif slot.thread_id is not None:
                    # 会话锁跟随租约（§4.4）
                    with contextlib.suppress(Exception):
                        await relay.renew_thread_lock(
                            slot.thread_id, owner=run_id, ttl_s=self._settings.run_lease_s
                        )
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    # ------------------------------------------------------------------ 交接

    async def drain(self) -> None:
        """停止领取，把在跑的 run 交给别的实例（§6）。可重复调用，只做一次。

        native 在下一个安全点（工具轮次落库之后）停下；acp 直接断开 —— 那一轮在 Pod 里
        照常进行，接手的实例 attach 回去。等不到安全点的（一个工具跑了很久）到
        drain_timeout_s 后直接中止，同样交接，最多丢这一轮。
        """
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain(), name="run-drain")
        await asyncio.shield(self._drain_task)

    async def _drain(self) -> None:
        self._draining = True
        # 等正在进行的那次领取结束：它领到的 run 也要在下面的名单里
        async with self._claim_lock:
            slots = list(self._slots.values())
        if slots:
            logger.info("停机：交接 %d 个在跑的 run", len(slots))
        for slot in slots:
            slot.drain = True
            if slot.acp and slot.task is not None:
                slot.task.cancel()
        tasks = [s.task for s in slots if s.task is not None and not s.task.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self._settings.drain_timeout_s)
            for task in pending:
                task.cancel()
            if pending:
                logger.warning("%d 个 run 没等到安全点，中止后交接", len(pending))
                await asyncio.wait(pending, timeout=10)
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._renewer

    async def _hand_off(self, run_id: UUID) -> None:
        """清掉租约（状态不动），唤醒别的实例立刻领走。"""
        try:
            async with self._sessionmaker() as session:
                released = await RunRepository(session).release_lease(run_id, owner=self._instance)
                await session.commit()
        except Exception:
            # 清不掉也不丢 run：租约到期后照样被接管，只是慢一个周期
            logger.warning("run %s 交接失败，等租约过期接管", run_id, exc_info=True)
            return
        if released:
            logger.info("run %s 已交接", run_id)
            await self._wake()

    # ------------------------------------------------------------------ 执行

    def _redis(self) -> aioredis.Redis:
        return make_redis(self._settings)

    async def _guarded(self, run_id: UUID, slot: _Slot, takeover: str | None = None) -> None:
        try:
            await self._execute(run_id, slot, takeover)
        except asyncio.CancelledError:
            # 停机交接、或被别的实例接管。前者清租约让别人立刻领走；后者什么都不能碰。
            # ★ 不再往上抛：取消是本执行器自己发起的，任务就此正常结束。
            if not slot.lost and not slot.settled:
                await self._hand_off(run_id)
        except LeaseLost:
            slot.lost = True
            logger.warning("run %s 的租约已被别的实例接管，本实例的这一段作废", run_id)
        except Exception:
            # 兜底：绝不让后台任务静默死掉，否则会话会永远停在 running
            logger.exception("run %s 执行时发生未捕获异常", run_id)
            if not slot.lost:
                with contextlib.suppress(Exception):
                    await self._force_fail(run_id)

    async def _execute(self, run_id: UUID, slot: _Slot, takeover: str | None = None) -> None:
        redis = self._redis()
        relay = EventRelay(redis, ttl_s=self._settings.run_events_ttl_s)
        #: 自上一个检查点以来发布的事件 —— 下一个检查点 / 收尾时归档
        collected: list[TraceEvent] = []
        #: 已分配出去的最后一个 run 内序号（检查点之后 collected 会清空，不能从它推）
        last_seq = 0
        # ★ 必须先初始化：load_for_execution 返回 None 时直接 return，
        #   而 return 要经过 finally —— 那里会读它。
        thread_id: UUID | None = None
        parent_run_id: UUID | None = None
        #: 这一段是挂起收尾的（还在等子 run），不是这一轮结束了。
        #: finally 里据此**不**释放会话锁、不清取消位 —— run 还没完。
        suspended = False
        #: 这一段被交接 / 取消了：run 没结束，锁与取消位都要留给接手的实例
        interrupted = False

        try:
            async with self._sessionmaker() as session:
                runs = RunRepository(session)
                loaded = await runs.load_for_execution(run_id)
                if loaded is None:
                    # ★ run 行在、会话或配置版本没了（会话被删）。判失败收掉 —— 否则它会
                    #   一遍遍被领取、一遍遍跳过
                    logger.error("run %s 的会话或配置版本不存在，判失败", run_id)
                    await runs.finish(
                        run_id,
                        status="failed",
                        last_seq=0,
                        usage={},
                        error_kind="orphaned",
                        error_message="会话或配置版本已不存在",
                        owner=self._instance,
                    )
                    await session.commit()
                    return
                run, thread, version = loaded
                thread_id = thread.id
                slot.thread_id = thread_id
                # ★ 出站身份（内网身份传递 §7.2）：这一轮发出的一切内网调用（知识库、配置服务、
                #   用户中心）都带它。执行任务是全新的上下文、不继承任何请求；接管的实例同样
                #   按 run 行重建。auth_uid 与发起人不同只会出现在这里：以 Agent 为权限时
                #   是数字员工（建 run 时按 Agent 的权限类型取好，数字员工设计 §7.3）。
                initiator = run.created_by or thread.created_by
                use_identity(CallIdentity(uid=initiator, auth_uid=run.auth_uid or initiator))
                parent_run_id = run.parent_run_id
                # ★ 事件流归属**根会话**：子 run 的事件也进父会话那条流，
                #   否则「一个会话看到所有事」不成立（见 Thread.stream_thread_id）。
                stream_thread_id = thread.stream_thread_id
                # Redis 的序号 key 丢失时的恢复水位。每段查一次，不是每事件。
                seq_floor = await runs.thread_seq_floor(stream_thread_id)

                resumed: TraceEvent | None = None
                if takeover is not None:
                    resumed = await self._recover(session, run, thread, relay, takeover)
                    # _recover 的 UPDATE 让 ORM 行过期了；异步会话不能懒加载，显式刷新
                    await session.refresh(run)
                prepared = await self._prepare(session, run, thread, version)
                if resumed is not None:
                    prepared = replace(
                        prepared,
                        start_seq=resumed.seq,
                        takeover=takeover,
                        acp_bridge_instance=run.acp_bridge_instance,
                        acp_acked_seq=run.acp_acked_seq,
                    )
                await runs.mark_running(run_id)
                await session.commit()

            slot.acp = prepared.spec.kind == "acp"
            last_seq = prepared.start_seq
            # 会话锁跟随租约（§4.4）：接管时原来的锁多半已经过期，重新占上
            with contextlib.suppress(Exception):
                await relay.renew_thread_lock(
                    thread_id, owner=run_id, ttl_s=self._settings.run_lease_s
                )
            if resumed is not None:
                collected.append(
                    await relay.publish(resumed, thread_id=stream_thread_id, floor=seq_floor)
                )
            if slot.drain and slot.acp:
                raise asyncio.CancelledError  # 领到时已在停机：直接交回去

            assistant_content: list[dict] | None = None
            usage: dict[str, int] = {}
            #: 这一段的收尾事件 —— 终态（run 结束）或 run.suspended（还要续）。
            segment_end: TraceEvent | None = None
            title: dict[str, Any] | None = None
            # ★ 本轮实际产生的消息（模型的 tool_use、工具的结果）。落库它们
            #   是分段执行的前提 —— 段与段之间没有别的载体（§7.4 的「没有
            #   checkpointer」在这里仍然成立：事实源始终是 message 表）。
            transcript = Transcript()
            #: native 走到了安全点、且本实例在停机：交接
            hand_off = False
            #: 本段里审批模式变了（计划获批）、还没落库的新值
            mode_changed: str | None = None

            # ★ 「一轮怎么跑」在 runtime 里（acp 详设 §02）。本方法的前后两段
            #   ——载入/标记运行中，与收事件/落库/释放锁——与 agent 类型无关，
            #   acp 接进来时一行都不用改。
            runtime = select_runtime(prepared, native=self._native, acp=self._acp)
            stream = runtime.run_turn(
                prepared, run_id=run_id, redis=redis, relay=relay, transcript=transcript
            )
            # ★ Run 是 trace 的根（可观测性设计 §04）。子 span 由事件流派生 ——
            #   native 与 acp 产出同形事件，所以这一处覆盖两条执行路径。
            #   未启用遥测时 RunTrace 里拿到的是 no-op span，零开销。
            with RunTrace.start(
                prepared,
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
            ) as run_trace:
                # ★ aclosing：交接时中途 break，生成器要被关掉（图停下、bridge 连接断开）
                async with contextlib.aclosing(stream):
                    async for event in stream:
                        run_trace.observe(event)

                        # ★ 收尾事件**先落库再发布**。否则客户端收到 run.finished 后
                        #   立刻 GET /runs/{id} 会读到 status=running、tokens=0 ——
                        #   而前端正是这么用的（收到终止事件就去取最终用量）。
                        #   这条顺序保证："看到收尾事件" ⇒ "DB 已是对应的状态"。
                        #   挂起同理：看到 run.suspended ⇒ DB 已是 suspended。
                        #
                        #   序号却要**现在**就盖上 —— 归档按 thread_seq 落库，而它是
                        #   主键的一半。分配与发布因此分成两步（relay.emit）。
                        if event.ends_segment:
                            segment_end = event.stamped(
                                await relay.next_thread_seq(stream_thread_id, floor=seq_floor)
                            )
                            collected.append(segment_end)
                            last_seq = segment_end.seq
                            break

                        # publish 的返回值必须用上 —— 会话级序号是它盖的
                        collected.append(
                            await relay.publish(event, thread_id=stream_thread_id, floor=seq_floor)
                        )
                        last_seq = event.seq
                        if event.type is EventType.MESSAGE_COMPLETED:
                            assistant_content = event.data.get("content")
                        elif event.type is EventType.USAGE_UPDATED:
                            usage = {k: v for k, v in event.data.items() if isinstance(v, int)}
                        elif event.type is EventType.TITLE_GENERATED:
                            title = dict(event.data)
                        elif (
                            event.type is EventType.AGENT_MODE
                            and event.data.get("source") == "plan_approved"
                        ):
                            # ★ 计划获批、模式变了：随下一次检查点（与那条工具结果同一个事务）
                            #   或段收尾落库。只在收尾时写的话，检查点之后进程死掉，接管者会按
                            #   旧的规划模式继续，而「计划已批准」的结果已经在消息里了
                            mode_changed = str(event.data.get("effective") or "") or None

                        # ★ 检查点（§5.1）：模型说完、或一批工具结果收齐，就落库。
                        #   进程在这之后死掉，接手的实例从这里接着跑。
                        if rows := transcript.checkpoint_rows():
                            await self._checkpoint(
                                run_id,
                                thread_id=thread_id,
                                stream_thread_id=stream_thread_id,
                                rows=rows,
                                events=collected,
                                last_seq=last_seq,
                                approval_mode=mode_changed,
                            )
                            collected = []
                            mode_changed = None
                        # ★ 安全点：本轮产生的都已落库、没有在跑的工具 —— 停在这里不丢任何工作
                        if (
                            slot.drain
                            and transcript.flushed == len(transcript.messages)
                            and not transcript.pending_calls()
                        ):
                            hand_off = True
                            break

            if hand_off:
                interrupted = True
                if collected:  # 安全点之后、交接之前发布的事件（通常没有）
                    with contextlib.suppress(Exception):
                        await self._checkpoint(
                            run_id,
                            thread_id=thread_id,
                            stream_thread_id=stream_thread_id,
                            rows=[],
                            events=collected,
                            last_seq=last_seq,
                            approval_mode=mode_changed,
                        )
                await self._hand_off(run_id)
                return

            suspended = segment_end is not None and segment_end.type is EventType.RUN_SUSPENDED

            # 从这里起是收尾：终态 / 挂起都会清掉租约，续租不再管它
            slot.settled = True
            await self._persist(
                run_id=run_id,
                thread_id=thread_id,
                stream_thread_id=stream_thread_id,
                events=collected,
                last_seq=last_seq,
                assistant_content=assistant_content,
                usage=usage,
                segment_end=segment_end,
                title=title,
                transcript=transcript,
                suspended=suspended,
                approval_mode=mode_changed,
            )
            if segment_end is not None:
                # 序号已在 break 处盖好 —— 这里只发布
                await relay.emit(segment_end, thread_id=stream_thread_id)

            if suspended:
                # ★ 必须在落库之后**再查一次** join barrier，否则有一个必然会
                #   踩到的竞态：子 run 可能在父这段收尾之前就跑完了，那一刻它
                #   去唤醒父 run，而父还是 running —— CAS 失败，唤醒丢掉；随后
                #   父落成 suspended，从此再没有人来叫它。
                #   委派越短这个窗口命中率越高（子 run 秒回时几乎必现）。
                await self.resume_if_ready(run_id)
                return

            # ★ 记忆不在 run 收尾时抽取：Agent 在对话中自己保存，漏记的由会话空闲后的补记补上
            #   （doc/memory-design.html §4.3；维护者调度，memory/backfill.py）。

            # ★ 这是个**子 run**，而且刚跑完 —— 去叫醒等着它的父 run。
            #   放在最后：父续跑会起一个新任务，不该让它排在本 run 的收尾前面。
            if parent_run_id is not None:
                await self.resume_if_ready(parent_run_id)
        except asyncio.CancelledError:
            interrupted = True
            raise
        except LeaseLost:
            slot.lost = True
            raise
        finally:
            # ★ suspend 不释放会话锁、不清取消位 —— 这一轮没结束。
            #
            #   锁要一直拿着：放了的话用户能在等待期间发第二条消息，两个 run
            #   交错写同一个 thread，「同一会话串行」就破了。
            #   取消位要留着：用户在等待期间点取消是完全正常的操作，清掉的话
            #   那次取消就静悄悄地没了。
            # ★ 交接 / 被接管同理：run 还在（别的实例上）继续，锁的 owner 就是它自己。
            if not suspended and not interrupted and not slot.lost:
                with contextlib.suppress(Exception):
                    await relay.clear_cancel(run_id)
                    if thread_id is not None:
                        # owner=run_id：锁若已过期并被下一个 run 持有，这里不会误删（H3）
                        await relay.release_thread_lock(thread_id, owner=run_id)
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _recover(
        self,
        session: AsyncSession,
        run: Run,
        thread: Thread,
        relay: EventRelay,
        takeover: str,
    ) -> TraceEvent:
        """接管一个中途换手的 run（§5）：把上一个实例留下的现场收拾好。返回 run.resumed。

            补归档    检查点之后发布到 Redis、还没归档的事件（流里有、库里没有的序号）
            续序号    run 内 seq 从上一个实例实际发到的地方接着数，不重号
            补结果    检查点里有 tool_use、没有结果的调用 → 「结果未知」（不盲目重放副作用）
            丢正文    最后一次模型调用已流出、没落库的正文字数 → 前端据此丢掉（会重做）

        与检查点同一个事务里做，第一条写是围栏：租约若已不在本实例就整个回滚。
        """
        runs = RunRepository(session)
        stream_thread_id = thread.stream_thread_id
        lost = await relay.run_events_after(stream_thread_id, run.id, after_seq=run.last_seq)
        top = max([run.last_seq, *(e.seq for e in lost)])
        await runs.checkpoint(run.id, owner=self._instance, last_seq=top)
        archived = await runs.archived_thread_seqs(run.id, [e.thread_seq for e in lost])
        await runs.archive_events(
            [e for e in lost if e.thread_seq not in archived], thread_id=stream_thread_id
        )
        # acp 的 message 表里没有 tool_use（工具在 Pod 里），这一步对它天然是空操作
        rows = await runs.history(thread.id, after=thread.summary_upto)
        if (sealing := interrupted_results(rows)) is not None:
            await ThreadRepository(session).add_messages(
                thread_id=thread.id, messages=[sealing], run_id=run.id
            )
            logger.warning("run %s 接管：%d 个工具调用结果未知", run.id, len(sealing.content))
        base_depth = run.depth
        return EventFactory(run.id, _utcnow, start_seq=top, base_depth=base_depth).make(
            EventType.RUN_RESUMED,
            {
                "reason": takeover,
                "discard_chars": _discarded_chars(lost, base_depth),
                "takeovers": run.takeovers,
            },
        )

    async def _checkpoint(
        self,
        run_id: UUID,
        *,
        thread_id: UUID,
        stream_thread_id: UUID,
        rows: list[StoredMessage],
        events: list[TraceEvent],
        last_seq: int,
        approval_mode: str | None = None,
    ) -> None:
        """一个工具轮次的检查点（§5.1）：消息 + 事件归档 + 序号，一个事务。

        approval_mode 非空 = 本轮里计划获批、模式变了：与「计划已批准」那条工具结果同一个事务落库。

        ★ 第一条写是围栏（checkpoint 校验 owner）：租约已不在本实例就抛 LeaseLost、
          整个回滚 —— 接管者与假死复活的旧实例不能双写同一个 run。
        """
        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            await runs.checkpoint(run_id, owner=self._instance, last_seq=last_seq)
            if rows:
                await ThreadRepository(session).add_messages(
                    thread_id=thread_id, messages=rows, run_id=run_id
                )
            if approval_mode:
                await _save_approval_mode(session, run_id, thread_id, approval_mode)
            await runs.archive_events(events, thread_id=stream_thread_id)
            await session.commit()

    async def resume_if_ready(self, run_id: UUID) -> bool:
        """等的东西都到了就把这个 suspended 的 run 续上。返回是否真的续了。

        ★ 三个调用点，同一套判据：子 run 跑完时、父自己挂起落库后（关竞态
          窗口）、周期 reaper 补扫时。做成一个方法是因为这三处漏掉任何一个
          都会让 run 永久卡在 suspended，而那是最难发现的故障形态 ——
          没有报错、没有事件，只是一个永远转圈的界面。

        ★ CAS 在 claim_for_resume 里。几个子 run 同时结束时它们都会走到这，
          只有一个能抢到。
        """
        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            run = await runs.get(run_id)
            # ★ 两个挂起态都要认。等审批的 run 落的是 awaiting_approval
            #   （suspended 的细分，为了让 UI 区分「等你」和「等机器」）——
            #   只认字面量 "suspended" 的话审批永远唤不醒，用户点了「允许」
            #   之后 run 一动不动。
            if run is None or run.status not in SUSPENDED_STATUSES:
                return False
            if not await self._waits_are_settled(session, run):
                return False
            if not await runs.claim_for_resume(run_id):
                return False  # 别人抢到了
            await session.commit()

        # ★ 用户在等待期间点了取消 —— 别再装一次图、起一次任务去发现这件事。
        #   续跑段的 runner 循环当然也会检查取消位，但那要等到第一次 astream
        #   迭代，中间白装配一次图（MCP 解析、对象存储连接、模型构造）。
        if await self._cancel_requested(run_id):
            logger.info("run %s 在等待期间被取消，不再续跑", run_id)
            await self._settle_cancelled(run_id)
            return False

        logger.info("run %s 等的都有结果了，续跑", run_id)
        await self.submit(run_id)
        return True

    async def _waits_are_settled(self, session: AsyncSession, run: Run) -> bool:
        """这个 run 等的东西都有结果了吗 —— 续跑的 barrier。

        ★ 按 reason 分派，不是一套判据打天下：

            subagent    子 run 进终态
            approval    approval 行被决策（approved / rejected / expired）

          用 SubAgent 那套去判审批的话，barrier 会**立刻满足**（审批挂起时没有子
          run），于是续跑 → 又挂起 → 再续跑，死循环。这是 run.waiting_on
          那一列存在的全部理由（迁移 0012）。

        ★ 等待项是 {tool_call_id: {reason, token}}（contracts/suspension.py）。没有等待项
          的挂起态一律不续跑：

            awaiting_approval  **acp 的在线等待**，run 其实还在跑（bridge 正等着那个
                               权限响应）。续跑它等于把同一个 run 执行两遍：第二条上游
                               连接会把第一个 superseded(4409)，整轮崩掉。这条与
                               suspended_runs 的过滤是同一个判据，两处都守 —— 主动唤醒
                               那一路不经过扫描
            其它               旧格式（数组）的遗留数据。不续跑，用户可以取消它
        """
        runs = RunRepository(session)
        waits = run.waiting_on if isinstance(run.waiting_on, dict) else {}
        if not waits:
            return False

        approvals = ApprovalRepository(session)
        for wait in waits.values():
            reason = (wait or {}).get("reason")
            token = (wait or {}).get("token") or ""
            if reason == "approval":
                # ★ 记录丢了算「已结清」，不是「继续等」：等一个不存在的审批 = run 永久
                #   挂起、会话一直被锁。续跑时按门禁故障告诉模型（_resume_state）。
                if await self._approval_decision(approvals, token) == "pending":
                    return False
            elif reason == "subagent":
                if await runs.has_unfinished_children(run.id):
                    return False
            else:
                # 不认识的 reason —— 不敢续跑（可能永远等不到），也不敢判死。
                # 记一条日志让它被发现，而不是静默卡住。
                logger.warning("run %s 的 waiting_on 里有未知 reason：%r", run.id, reason)
                return False
        return True

    async def _cancel_requested(self, run_id: UUID) -> bool:
        redis = self._redis()
        try:
            return await EventRelay(redis).is_cancelled(run_id)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _settle_cancelled(self, run_id: UUID) -> None:
        """把一个在等待中被取消的 run 收干净：落终态、发事件、放锁。

        ★ 这条收尾路径是 suspend 独有的。别处的取消都发生在**有进程在跑**
          的时候（runner 的循环自己发 run.cancelled 然后走正常收尾），而这里
          没有任何进程 —— 不自己收的话 run 会一直躺在 suspended，会话锁也
          一直不放。
        """
        redis = self._redis()
        relay = EventRelay(redis, ttl_s=self._settings.run_events_ttl_s)
        try:
            async with self._sessionmaker() as session:
                runs = RunRepository(session)
                run = await runs.get(run_id)
                if run is None:
                    return
                thread = await ThreadRepository(session).by_id(run.thread_id)
                if thread is None:
                    return
                stream_thread_id = thread.stream_thread_id

                event = EventFactory(
                    run_id,
                    _utcnow,
                    start_seq=run.last_seq,
                    base_depth=run.depth,
                ).make(EventType.RUN_CANCELLED, {"partial_text_len": 0})
                # 序号在落库前盖好，与 _execute 的收尾同一条纪律
                event = event.stamped(
                    await relay.next_thread_seq(
                        stream_thread_id,
                        floor=await runs.thread_seq_floor(stream_thread_id),
                    )
                )
                await runs.finish(
                    run_id,
                    status="cancelled",
                    last_seq=event.seq,
                    usage={},  # 累加语义下空字典 = 不动账
                )
                await runs.archive_events([event], thread_id=stream_thread_id)
                await session.commit()
                thread_id = run.thread_id

            await relay.emit(event, thread_id=stream_thread_id)
            with contextlib.suppress(Exception):
                await relay.clear_cancel(run_id)
                await relay.release_thread_lock(thread_id, owner=run_id)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _prepare(
        self,
        session: AsyncSession,
        run: Run,
        thread: Thread,
        version: AgentVersion,
    ) -> PreparedRun:
        """从库里还原「任务是什么」（spec / 输入 / 历史 / 摘要边界）。"""
        from ..repositories.agent import AgentRepository

        found = await AgentRepository(session).get(thread.agent_id)
        # agent 行理论上必然存在（thread.agent_id 有外键），兜底只为不炸
        slug, name = (found[0].slug, found[0].name) if found is not None else ("agent", "agent")

        spec = _spec_from_version(version, slug=slug, name=name)

        # ★ 子会话的 run：从父 agent 的同一份快照里派生出子智能体自己的
        #   AgentSpec。子会话因此走与普通 run **完全同一条**链路 ——
        #   历史重建、压缩、步数刹车、审批、取消、孤儿回收全部原样复用。
        if thread.subagent_name:
            spec = spec_for_subagent(spec, thread.subagent_name)

        # 审批模式（doc/native-approval-mode-design.html）：run 上记的是建 run 时
        # （或计划获批后）的模式，子 run 记的是派活那一刻父 run 的模式。
        # native 按智能体允许的范围校正；acp 原样交下去，由 HostRuntime 与它自己的
        # permission_mode 取更严格的
        approval_mode = (
            spec.approval.resolve(run.approval_mode or thread.approval_mode)
            if spec.kind == "native"
            else run.approval_mode
        )

        rows = await RunRepository(session).history(thread.id, after=thread.summary_upto)
        if not rows:
            return PreparedRun(
                spec=spec,
                input_content="",
                thread=thread,
                agent_version_id=version.id,
                approval_mode=approval_mode,
            )

        # ★ 本轮输入是最后一条 **kind='chat' 的用户消息**，不是最后一条消息。
        #   工具结果在 Anthropic 的格式里同样是 role='user'（domain/messages.py），
        #   不加区分就会把一批工具结果当成用户的新提问 —— 表现是模型莫名回答
        #   一段 JSON，而用户真正的问题被归进了历史。
        input_index = _last_chat_input(rows)

        # ★ 输入消息之后还有东西 = 这个 run 之前已经跑过一段（挂起后续跑）。
        #   那时本轮输入早已在历史里，再 append 一遍会让模型看到同一个问题
        #   问了两次。判据取自数据本身，不靠额外的标记位。
        resume = input_index is not None and input_index < len(rows) - 1

        if input_index is None or resume:
            history = from_storage(rows)
            input_content: Any = ""
            upto = rows[-1].created_at
        else:
            history = from_storage(rows[:input_index])
            input_content = rows[input_index].content
            upto = rows[input_index - 1].created_at if input_index > 0 else None

        # ★ 续跑：消息原样交回、**不改写**，挂起现场（waiting_on + decisions）另交。
        #   被拦下的调用在消息里悬空，图内的中间件按 waiting_on 认出它在等什么、
        #   按 decisions 执行或填结论（contracts/suspension.py）。
        waiting_on, decisions = await self._resume_state(session, run, history)

        # §7.4：已压缩的早期消息以摘要形式回填，而不是重新送原文。
        # 没有这一步，每个 run 都要重压一次 —— 摘要 token 重复付、
        # prompt cache 每轮击穿（§7.3 坑 2 的抖动）。
        if thread.summary:
            history.insert(0, AIMessage(content=SUMMARY_PREFIX + thread.summary))

        return PreparedRun(
            spec=spec,
            input_content=input_content,
            history=history,
            history_upto=upto,
            thread=thread,
            agent_version_id=version.id,
            resume=resume,
            # 本轮用户消息的时间：Agent 这一轮写入记忆时的「信息成立时间」（记忆设计 §6.4）
            input_at=rows[input_index].created_at if input_index is not None else None,
            start_seq=run.last_seq,
            prior_tokens=run.total_tokens,
            # ★ 子孙 run 的事件带它在 run 树里的深度（子 1、孙 2 ……），建子 run 时记下
            #   （迁移 0021）。SubAgent 可以继续派 SubAgent，不能再按「有没有父 run」判 0 / 1。
            base_depth=run.depth,
            waiting_on=waiting_on,
            decisions=decisions,
            approval_mode=approval_mode,
        )

    async def _resume_state(
        self, session: AsyncSession, run: Run, history: list[BaseMessage]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """续跑交回图的挂起现场：(还在等的调用, 已经有的结果)。

        ★ 两份依据必须一起交回（demo/suspend-resume 要点 1）：消息里只看得出某个调用
          还没结果，看不出它在等什么。漏了 waiting_on，图入口的 Patch 会把「在等」当成
          「被放弃」补一句 cancelled，模型照着回答，被批准的工具永远不执行。

        ★ 剪掉历史里已经有结果的项。run.waiting_on 要留到 run 结束（续跑段可能被任意
          实例领取，只能从库里读），这期间检查点会先把结果落进 message 表；接管时还会
          给悬空的调用补「结果未知」。这些都已经不在等了 —— 交给图的话，before_model
          会为它们跳 tools，而 ToolNode 执行的是**最后一条** AIMessage 的调用。

        ★ decisions 只放已经有结果的（contracts/suspension.py）：

            approval    ApprovalOutcome：approval 行的状态，加上计划审批选的模式、拒绝原因；
                        pending 不放；行丢了按 "failed"（门禁故障）——
                        关卡已判它结清，图里却拿不到决策的话，图跳出、关卡放行，死循环
            subagent    子 run 的结论文本（关卡保证子 run 都已终态）
        """
        waits = run.waiting_on if isinstance(run.waiting_on, dict) else {}
        answered = {m.tool_call_id for m in history if isinstance(m, ToolMessage)}
        waits = {cid: w for cid, w in waits.items() if cid not in answered and isinstance(w, dict)}
        decisions: dict[str, Any] = {}
        approvals = ApprovalRepository(session)
        for wait in waits.values():
            token = str(wait.get("token") or "")
            if wait.get("reason") == "approval":
                outcome = await self._approval_outcome(approvals, token)
                if outcome["status"] != "pending":
                    decisions[token] = outcome
            elif wait.get("reason") == "subagent":
                try:
                    decisions[token] = await subagent_result(session, UUID(token))
                except ValueError:  # token 不是个合法 UUID —— 不该发生
                    logger.warning("waiting_on 里的 sub_run_id 非法：%r", token)
                    decisions[token] = "SubAgent 的子 run 记录无法解析，本轮拿不到它的结论。"
            else:
                logger.warning("run %s 的 waiting_on 里有未知 reason：%r", run.id, wait)
        return waits, decisions

    @staticmethod
    async def _approval_decision(approvals: ApprovalRepository, token: str) -> str:
        """approval 行的状态；行丢了（或 token 非法）按 "failed"。"""
        try:
            row = await approvals.get(UUID(token))
        except ValueError:
            logger.warning("waiting_on 里的 approval token 非法：%r", token)
            return "failed"
        return "failed" if row is None else row.status

    @staticmethod
    async def _approval_outcome(approvals: ApprovalRepository, token: str) -> dict[str, Any]:
        """续跑交给图的审批结果（contracts/permission.py::ApprovalOutcome）。

        status 之外，计划审批带回用户选的执行模式，拒绝带回原因，问题带回答案（approval.outcome）。
        行丢了（或 token 非法）按 "failed"。
        """
        try:
            row = await approvals.get(UUID(token))
        except ValueError:
            logger.warning("waiting_on 里的 approval token 非法：%r", token)
            return {"status": "failed"}
        if row is None:
            return {"status": "failed"}
        extra = row.outcome if isinstance(row.outcome, dict) else {}
        picked = {k: extra[k] for k in ("mode", "feedback", "answers") if extra.get(k)}
        return {"status": row.status, **picked}

    async def _persist(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        stream_thread_id: UUID,
        events: list[TraceEvent],
        assistant_content: list[dict] | None,
        usage: dict[str, int],
        segment_end: TraceEvent | None,
        last_seq: int = 0,
        title: dict[str, Any] | None = None,
        transcript: Transcript | None = None,
        suspended: bool = False,
        approval_mode: str | None = None,
    ) -> None:
        """一段的收尾落库。events 是**上一个检查点之后**的事件，之前的已经归档过了。

        approval_mode 非空 = 上一个检查点之后计划获批、模式变了，随收尾一起落库。"""
        terminal = None if suspended else segment_end
        status = _STATUS_BY_TERMINAL.get(terminal.type, "failed") if terminal else "failed"
        error_kind = None
        error_message = None
        if terminal is not None and terminal.type is EventType.RUN_FAILED:
            error_kind = terminal.data.get("error_kind")
            error_message = str(terminal.data.get("message", ""))[:2000]
        elif terminal is None and not suspended:
            error_kind = "interrupted"
            error_message = "事件流未产出终止事件"
        # 挂起时还在等的调用：事件里是数组，落库还原成 state 的形状 {tool_call_id: Wait}
        waits = (segment_end.data.get("waiting_on") or []) if suspended and segment_end else []
        waiting_on = {
            str(wait["tool_call_id"]): {"reason": wait.get("reason"), "token": wait.get("token")}
            for wait in waits
            if isinstance(wait, dict) and wait.get("tool_call_id")
        }

        async with self._sessionmaker() as session:
            runs = RunRepository(session)
            threads = ThreadRepository(session)
            # ★ 有 transcript 就落**完整序列**（模型说的每一轮 + 每个工具的
            #   结果），没有就退回只落最终正文。
            #
            #   两条路的分界不是「native / acp」而是「拿不拿得到忠实的序列」：
            #   acp 的工具调用发生在 Pod 内部，平台侧拼不出来，于是它走后一条
            #   （它的上下文恢复本来也不靠 message 表）。
            #
            #   ★ 落库与终态无关 —— 失败的 run 也落。模型说过的话、调过的工具
            #     是**已经发生的事实**；抹掉它们的代价是用户重试时模型不知道
            #     上次做到哪，于是把有副作用的操作又做一遍。中断的工具调用由
            #     to_storage 补上 is_error 的结果，格式始终是合法的。
            #
            # ★ 只落检查点之后的部分（remaining_rows）：之前的已经在检查点里落过了。
            #
            # ★ 挂起时还在等的调用不补「中断」—— 它们故意悬空，与下面的 run.waiting_on
            #   在**同一个事务**里落库，续跑时一起交回（contracts/suspension.py）。
            if transcript:
                await threads.add_messages(
                    thread_id=thread_id,
                    messages=transcript.remaining_rows(waiting=waiting_on),
                    run_id=run_id,
                )
            elif assistant_content:
                await threads.add_message(
                    thread_id=thread_id,
                    role="assistant",
                    content=assistant_content,
                    run_id=run_id,
                )
            if approval_mode:
                await _save_approval_mode(session, run_id, thread_id, approval_mode)
            if title is not None:
                # WHERE title_source != 'manual' 在仓库里，用户改过的名字不会被覆盖
                await threads.set_generated_title(
                    thread_id,
                    title=str(title.get("title", "")),
                    degraded=bool(title.get("degraded")),
                )
            last_seq = max(last_seq, events[-1].seq if events else 0)
            # ★ 围栏（owner=本实例）：租约已被接管就抛 LeaseLost，整个收尾回滚（§13）
            if suspended:
                # ★ 「在等什么」必须落库：续跑的 barrier 按 reason 分派，只记
                #   token 的话审批挂起会被当成 SubAgent → barrier 立刻满足 → 死循环。
                await runs.suspend(
                    run_id,
                    last_seq=last_seq,
                    usage=usage,
                    waiting_on=waiting_on,
                    owner=self._instance,
                )
            else:
                await runs.finish(
                    run_id,
                    status=status,
                    last_seq=last_seq,
                    usage=usage,
                    error_kind=error_kind,
                    error_message=error_message,
                    owner=self._instance,
                )
            # ★ 事件逐段归档。Redis 的 Stream 有 TTL，而一个 run 可能横跨
            #   一小时以上的等待 —— 前几段的事件不当场落库的话，用户在委派
            #   返回后刷新页面，看到的是一段从中间开始的历史。
            #
            # ★ 归档按**根会话**分组（stream_thread_id），与事件流同一个口径。
            #   子 run 的事件因此归到父会话名下 —— 回放一条会话流时它们必须
            #   在里面。
            await runs.archive_events(events, thread_id=stream_thread_id)
            if not suspended:
                # 归档之后再汇总：这一段的 skill.loaded 也要算进去（跨段的 run 只算一次）
                await SkillUsageRepository(session).record_run(
                    run_id, succeeded=status == "succeeded", day=_today()
                )
            await runs.touch_thread(thread_id)
            await session.commit()

    async def _force_fail(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            await RunRepository(session).finish(
                run_id,
                status="failed",
                last_seq=0,
                usage={},
                error_kind="internal_error",
                error_message="执行器内部错误",
                # 围栏：已被别的实例接管的 run 不能被这里判死
                owner=self._instance,
            )
            await session.commit()
