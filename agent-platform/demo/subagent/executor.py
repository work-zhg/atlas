"""执行器（server 侧）：跑一段、落库、判断能否续跑、组装续跑的入参。

所有 run 走同一条路，根 run 和子 run 的唯一区别是有没有 parent_run_id：

    一段    建图 → ainvoke(入参) → 消息 + waiting_on 一起落库
            waiting_on 非空就是挂起：执行它的任务就此结束，进程可以退出
    结束    （成功或失败）有父 run 就去叫醒它
    叫醒    barrier（等的子 run 都结束了吗）→ CAS 抢续跑权 → 提交下一段

SubAgent 派 SubAgent 时，这条路一层层叠起来：
孙 run 结束叫醒子 run，子 run 续跑、结束后再叫醒根 run。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents import build_agent
from scripted import first_line
from store import TERMINAL, Run, Store
from subagent_service import SubAgentService, result_text
from tracing import enter, say


class Executor:
    """submit 就起一个任务跑这个 run 的一段。真实系统是领取队列 + 租约，进程重启也能接着领。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._tasks: set[asyncio.Task] = set()  # 持有引用，否则任务可能被垃圾回收

    def submit(self, run_id: str) -> None:
        task = asyncio.create_task(self._execute(run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute(self, run_id: str) -> None:
        if not await self.store.claim(run_id):
            return
        run = await self.store.get_run(run_id)
        enter(run.id, run.segments)
        if await self._run_segment(run) == "suspended":
            # ★ 落库之后必须自己再查一次：子 run 可能在落库之前就结束了，它来叫醒时这个 run
            #   还是 running，那次唤醒就落空了，之后再没人来叫（README 实验 4）
            await self.resume_if_ready(run.id, why="落库后复查")
        elif run.parent_run_id is not None:
            # ★ 这是个子 run，刚结束（成功或失败）—— 去叫醒派出它的那个 run
            await self.resume_if_ready(run.parent_run_id, why="收尾时叫醒")

    async def _run_segment(self, run: Run) -> str:
        """跑一段，返回落库后的状态：suspended / succeeded / failed。"""
        name = self.store.threads[run.thread_id].subagent_name
        payload = await segment_payload(self.store, run)
        if run.segments == 1:
            say(f"{name or '主智能体'} 开跑「{payload['messages'][-1].content}」")
        else:
            waits, results = list(payload.get("waiting_on", {})), list(payload["results"])
            n = len(payload["messages"])
            say(f"续跑（全新的图）：消息 {n} 条 + waiting_on {waits} + results {results}")
        agent = build_agent(name, SubAgentService(self.store, self, run))
        try:
            result = await agent.ainvoke(payload)
        except Exception as exc:  # 这个 run 失败是它自己的结局，不该炸到执行器
            await self.store.fail(run.id, str(exc))
            say(f"失败：{exc}")
            return "failed"
        messages, waiting_on = result["messages"], result.get("waiting_on") or None
        status = await self.store.save_segment(run.id, messages=messages, waiting_on=waiting_on)
        if status == "suspended":
            say(f"落库：消息 {len(messages)} 条 + waiting_on {list(waiting_on or {})} → suspended")
        else:
            say(f"落库 → 结束：{first_line(messages[-1].content)}")
        return status

    # ------------------------------------------------------------------ 叫醒
    async def resume_if_ready(self, run_id: str, *, why: str) -> bool:
        """等的都到了就把挂起的 run 续上。

        调用点有两个：子 run 结束时、自己挂起落库后复查（真实系统还有周期补扫）。
        漏掉任何一个，run 都可能永远停在 suspended —— 没有报错，只是一个一直转圈的界面。
        """
        run = await self.store.get_run(run_id)
        if run.status != "suspended":
            return False  # 还没挂起（正在落库），或者已经被别人续上了
        pending = await self.pending_waits(run)
        if pending:
            say(f"{why} {run_id}：还在等 {pending}，不续")
            return False
        if not await self.store.claim_for_resume(run_id):
            say(f"{why} {run_id}：CAS 没抢到，别人已经续上了")
            return False
        say(f"{why} {run_id}：等的子 run 都结束了，CAS 抢到 → 提交下一段")
        self.submit(run_id)
        return True

    async def pending_waits(self, run: Run) -> list[str]:
        """续跑的 barrier：按 reason 判断，返回还没等到的 token。"""
        pending = []
        for wait in (run.waiting_on or {}).values():
            if wait["reason"] != "subagent":
                pending.append(wait["token"])  # 不认识的 reason：不敢续跑
            elif (await self.store.get_run(wait["token"])).status not in TERMINAL:
                pending.append(wait["token"])
        return pending


async def segment_payload(store: Store, run: Run) -> dict[str, Any]:
    """组装这一段的入参。消息原样交回，不改写。

    ★ 续跑时三样缺一不可，所以只在这一个函数里组装：
      messages    对话历史，里面那批 task 调用还悬空着
      waiting_on  悬空的调用在等哪个子 run —— 漏了它，Patch 会把它们当成被放弃、
                  补上 cancelled，子 run 的结论就丢了（README 实验 2）
      results     子 run 的结论。★ 只放已经结束的：before_model 看到 token 在 results 里
                  就会跳去 tools
    """
    messages = await store.history(run.thread_id)
    waits = run.waiting_on or {}
    if not waits:
        return {"messages": messages}
    results = {}
    for wait in waits.values():
        text = await result_text(store, wait["token"])
        if text is not None:
            results[wait["token"]] = text
    return {"messages": messages, "waiting_on": waits, "results": results}
