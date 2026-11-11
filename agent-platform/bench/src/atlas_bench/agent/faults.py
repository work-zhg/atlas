"""运行中的动作（doc/sandbox-eng-cases.html §4）：跟着会话事件流，在约定的时刻注入故障。

一个 trial 一个 FaultInjector：
    start()      没有触发条件的动作立即排上；有 deadline_s 的同时排一个兜底计时
    observe(e)   会话流上的每条事件都过一遍：满足某个动作的触发条件（第 nth 次）就排上它
    settle()     等已经排上的动作做完（发下一轮之前、收尾之前）
    restore()    把还没恢复的故障恢复掉（暂停的进程、断开的网络、缩掉的副本）—— 无论 trial 怎么
                 结束都要调，不能把测试环境留在故障状态

★ 按事件触发而不按绝对时间：故障每次都打在同一个位置（同一条命令跑到第 N 秒），结果才可比。
★ 动作做不成（kubectl 报错、找不到沙箱）记 ok=False：故障没注入，这次 trial 测不到要测的东西，
  runner 记评测侧错误、续跑时重做。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .cluster import ClusterOps
from .marks import command_of
from .schema import Action, Trigger

__all__ = ["FaultInjector", "TrialContext"]

logger = logging.getLogger(__name__)


class TrialContext(Protocol):
    """注入动作要用到的现场：当前 run、会话、会话的沙箱 Pod、取消 run。由平台适配器提供。"""

    def current_run(self) -> str: ...

    def session(self) -> str: ...

    async def sandbox_pod(self, mark: str | None) -> str: ...

    async def cancel_run(self, run_id: str) -> None: ...


def _matches(trigger: Trigger, event: dict[str, Any]) -> bool:
    kind = event.get("type")
    # CLI 先报工具名、命令随后补全：带 mark 的 tool.started 触发也认 tool.updated
    late_args = trigger.event == "tool.started" and trigger.mark is not None
    if kind != trigger.event and not (late_args and kind == "tool.updated"):
        return False
    data = event.get("data") or {}
    if trigger.tool is not None and data.get("name") != trigger.tool:
        return False
    if trigger.mark is not None:
        command = command_of(data.get("args"))
        if not re.search(rf"mark\.sh\s+{re.escape(trigger.mark)}\b", command):
            return False
    return True


@dataclass
class _Slot:
    action: Action
    seen: int = 0
    fired: bool = False
    #: 已经数过的工具调用：同一个调用的开始与参数补全只算一次
    calls: set[str] = field(default_factory=set)


@dataclass
class FaultInjector:
    actions: list[Action]
    ops: ClusterOps | None
    ctx: TrialContext
    records: list[dict[str, Any]] = field(default_factory=list)
    _slots: list[_Slot] = field(default_factory=list)
    _tasks: set[asyncio.Future[None]] = field(default_factory=set)
    _deadlines: set[asyncio.Future[None]] = field(default_factory=set)
    _t0: float = field(default_factory=time.monotonic)
    #: 还没恢复的故障：键 → 恢复动作（暂停的 Pod、下发的 NetworkPolicy、缩掉的副本）
    _undo: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    _paused: list[str] = field(default_factory=list)
    _blocked: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._slots = [_Slot(a) for a in self.actions]

    # ------------------------------------------------------------------ 触发

    def start(self) -> None:
        self._t0 = time.monotonic()
        for slot in self._slots:
            if slot.action.when is None:
                self._schedule(slot, None)
            elif slot.action.deadline_s is not None:
                self._spawn(self._deadline(slot), deadline=True)

    def observe(self, event: dict[str, Any]) -> None:
        for slot in self._slots:
            when = slot.action.when
            if slot.fired or when is None or not _matches(when, event):
                continue
            if when.event == "tool.started":
                call_id = str((event.get("data") or {}).get("call_id") or "")
                if call_id and call_id in slot.calls:
                    continue
                slot.calls.add(call_id)
            slot.seen += 1
            if slot.seen >= when.nth:
                self._schedule(slot, event)

    def pending(self) -> int:
        """还没触发的动作数（场景前提：注入都做了）。"""
        return sum(1 for s in self._slots if not s.fired)

    async def settle(self) -> None:
        while busy := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*busy, return_exceptions=True)

    async def close(self) -> None:
        """收尾：取消还没触发的兜底计时、等进行中的动作做完、恢复全部故障。"""
        for t in list(self._deadlines):
            t.cancel()
        await self.settle()
        await self.restore()

    async def restore(self) -> None:
        for key, undo in list(self._undo.items()):
            try:
                await undo()
                self._record(f"restore:{key}", "恢复故障", True, "")
            except Exception as exc:
                self._record(f"restore:{key}", "恢复故障", False, str(exc))
                logger.error("恢复故障 %s 失败，请手动处理：%s", key, exc)
            self._undo.pop(key, None)

    def _schedule(self, slot: _Slot, event: dict[str, Any] | None) -> None:
        slot.fired = True
        self._spawn(self._fire(slot.action, event))

    def _spawn(self, coro: Awaitable[None], *, deadline: bool = False) -> None:
        task = asyncio.ensure_future(coro)
        # 兜底计时不算「进行中的动作」：settle 不等它，close 时直接取消
        group = self._deadlines if deadline else self._tasks
        group.add(task)
        task.add_done_callback(group.discard)

    async def _deadline(self, slot: _Slot) -> None:
        assert slot.action.deadline_s is not None
        await asyncio.sleep(max(0.0, self._t0 + slot.action.deadline_s - time.monotonic()))
        if not slot.fired:
            logger.info("动作 %s 到了兜底时间仍未触发，照样执行", slot.action.do)
            self._schedule(slot, None)

    # ------------------------------------------------------------------ 执行

    async def _fire(self, action: Action, event: dict[str, Any] | None) -> None:
        trigger = (
            {"type": event.get("type"), "ts": event.get("ts"),
             "at_s": round(time.monotonic() - self._t0, 1)}
            if event is not None
            else None
        )  # fmt: skip
        if action.after_s:
            await asyncio.sleep(action.after_s)
        try:
            detail = await self._do(action, event)
            self._record(action.do, action.desc, True, detail, trigger)
        except Exception as exc:
            self._record(action.do, action.desc, False, str(exc), trigger)
            logger.warning("注入 %s 没做成：%s", action.do, exc)

    def _record(
        self, do: str, desc: str, ok: bool, detail: str, trigger: dict[str, Any] | None = None
    ) -> None:
        """at_s：动作做完的时刻；trigger：触发它的事件（类型、事件时间）—— 事后核对时机。"""
        self.records.append(
            {
                "do": do,
                "desc": desc,
                "at_s": round(time.monotonic() - self._t0, 1),
                "ok": ok,
                "detail": detail[:500],
                "trigger": trigger,
            }
        )

    def _cluster(self) -> ClusterOps:
        if self.ops is None:
            raise RuntimeError("这个动作要操作集群：评测没有配置集群操作（kubectl）")
        return self.ops

    async def _do(self, action: Action, event: dict[str, Any] | None) -> str:
        run_id = str((event or {}).get("run_id") or "") or self.ctx.current_run()
        mark = action.when.mark if action.when is not None else None
        match action.do:
            case "cancel_run":
                # 取消主 run（子 run 的事件也会触发它，取消要落在用户能取消的那一层）
                await self.ctx.cancel_run(self.ctx.current_run())
                return f"run {self.ctx.current_run()}"
            case "kill_server":
                pod = await self._cluster().owner_pod(self.ctx.current_run() or run_id)
                await self._cluster().kill_server(pod)
                return pod
            case "restart_server":
                await self._cluster().rollout_restart()
                return "rollout restart"
            case "pause_server":
                ops = self._cluster()
                pod = await ops.owner_pod(self.ctx.current_run() or run_id)
                await ops.pause(pod)
                self._paused.append(pod)
                self._undo[f"pause:{pod}"] = lambda: ops.resume(pod)
                return pod
            case "resume_server":
                ops = self._cluster()
                resumed = []
                while self._paused:
                    pod = self._paused.pop()
                    await ops.resume(pod)
                    self._undo.pop(f"pause:{pod}", None)
                    resumed.append(pod)
                return ",".join(resumed) or "（没有暂停中的实例）"
            case "delete_sandbox":
                pod = await self.ctx.sandbox_pod(action.args.get("mark") or mark)
                graceful = bool(action.args.get("graceful"))
                await self._cluster().delete_sandbox(pod, graceful=graceful)
                return f"{pod}（{'优雅删除' if graceful else '强删'}）"
            case "kill_cli":
                pod = await self.ctx.sandbox_pod(action.args.get("mark") or mark)
                killed = await self._cluster().kill_cli(pod, self.ctx.session())
                return f"{pod}：{killed}"
            case "block_sandbox":
                ops = self._cluster()
                pod = await self.ctx.sandbox_pod(action.args.get("mark") or mark)
                name = await ops.block(pod)
                self._blocked.append(name)
                self._undo[f"block:{name}"] = lambda: ops.unblock(name)
                return f"{pod} ← {name}"
            case "unblock_sandbox":
                ops = self._cluster()
                done = []
                while self._blocked:
                    name = self._blocked.pop()
                    await ops.unblock(name)
                    self._undo.pop(f"block:{name}", None)
                    done.append(name)
                return ",".join(done) or "（没有断开的网络）"
            case "scale_lifecycle":
                ops = self._cluster()
                replicas = int(action.args.get("replicas", 0))
                if "scale" not in self._undo:  # 记下最初的副本数，收尾时恢复成它
                    original = await ops.replicas()
                    self._undo["scale"] = lambda: ops.scale(original)
                await ops.scale(replicas)
                return f"replicas={replicas}"
        raise ValueError(f"不认识的动作 {action.do}")
