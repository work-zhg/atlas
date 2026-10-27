"""外部存储：thread / run / message 三张表（内存模拟）。

真实系统里是数据库，所有实例共用，进程重启后还在。每个方法相当于一条 SQL 或一个事务：
先往返一次（await，让出给别的协程），再原子地执行。并发的协程就在这些 await 处交错，
所以「落库后复查」「CAS」防的那些竞态，在 demo 里是真的会发生的。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from langchain_core.messages import BaseMessage, HumanMessage

DB_RT_S = 0.001  # 一次数据库往返
TERMINAL = {"succeeded", "failed"}


@dataclass
class Thread:
    id: str
    title: str
    parent_thread_id: str | None = None
    subagent_name: str | None = None  # 非空 = SubAgent 的会话


@dataclass
class Run:
    id: str
    thread_id: str
    parent_run_id: str | None = None  # 非空 = 子 run，指回派出它的那个 run
    # queued → running → succeeded / failed；或 running → suspended → queued → running → …
    status: str = "queued"
    # 挂起时在等什么：tool_call_id -> {"reason": "subagent", "token": 子 run id}
    # ★ 和消息一样是续跑的依据，必须和消息一起落库、一起交回图（demo/suspend-resume 要点 1）
    waiting_on: dict[str, dict[str, str]] | None = None
    error: str | None = None
    segments: int = 0  # 跑了几段：每挂起、续跑一次多一段


class Store:
    def __init__(self) -> None:
        self.threads: dict[str, Thread] = {}
        self.runs: dict[str, Run] = {}
        self.messages: dict[str, list[BaseMessage]] = {}  # thread_id -> 消息
        self._seq = {"t": 0, "r": 0}

    async def _rt(self) -> None:
        await asyncio.sleep(DB_RT_S)

    def _new_id(self, prefix: str) -> str:
        self._seq[prefix] += 1
        return f"{prefix}{self._seq[prefix]}"

    # ------------------------------------------------------------------ 会话与消息
    async def create_thread(self, title: str) -> Thread:
        await self._rt()
        thread = Thread(self._new_id("t"), title)
        self.threads[thread.id] = thread
        self.messages[thread.id] = []
        return thread

    async def add_message(self, thread_id: str, message: BaseMessage) -> None:
        await self._rt()
        self.messages[thread_id].append(message)

    async def history(self, thread_id: str) -> list[BaseMessage]:
        await self._rt()
        return list(self.messages[thread_id])

    # ------------------------------------------------------------------ run
    async def create_run(self, thread_id: str) -> Run:
        await self._rt()
        run = Run(self._new_id("r"), thread_id)
        self.runs[run.id] = run
        return replace(run)

    async def create_child(self, run_id: str, *, parent: Run, name: str, task: str) -> bool:
        """一个事务：建 SubAgent 的会话、子 run、写入任务书。建了返回 True。

        ★ run_id 已存在就什么都不做，返回 False（真实系统靠主键冲突）。同一段被重放时
          （比如上一次落库前进程崩了），同一个 tool_call 因此不会派出第二个子 run。
        """
        await self._rt()
        if run_id in self.runs:
            return False
        thread = Thread(
            self._new_id("t"),
            f"SubAgent · {name}",
            parent_thread_id=self.runs[parent.id].thread_id,
            subagent_name=name,
        )
        self.threads[thread.id] = thread
        # 任务书 = 子会话的第一条用户消息。父会话的历史一条都不带过去
        self.messages[thread.id] = [HumanMessage(task)]
        self.runs[run_id] = Run(run_id, thread.id, parent_run_id=parent.id)
        return True

    async def get_run(self, run_id: str) -> Run:
        await self._rt()
        return replace(self.runs[run_id])  # 读到的是这一刻的快照

    async def claim(self, run_id: str) -> bool:
        """领取：queued → running。真实系统是 SELECT … FOR UPDATE SKIP LOCKED。"""
        await self._rt()
        run = self.runs[run_id]
        if run.status != "queued":
            return False
        run.status, run.segments = "running", run.segments + 1
        return True

    async def save_segment(
        self, run_id: str, *, messages: list[BaseMessage], waiting_on: dict | None
    ) -> str:
        """一段结束：消息、waiting_on、状态在一个事务里写下。返回新状态。"""
        await self._rt()
        run = self.runs[run_id]
        self.messages[run.thread_id] = list(messages)
        run.waiting_on = waiting_on or None
        run.status = "suspended" if waiting_on else "succeeded"
        return run.status

    async def fail(self, run_id: str, error: str) -> None:
        await self._rt()
        run = self.runs[run_id]
        run.status, run.error = "failed", error

    async def claim_for_resume(self, run_id: str) -> bool:
        """★ CAS：UPDATE run SET status='queued' WHERE id=? AND status='suspended'。

        几个子 run 同时结束会同时来叫醒父 run。先查后改的话它们都看到 suspended，
        各提交一次，同一段就跑两遍。WHERE 里那个 status 才是互斥，影响行数是回执。

        waiting_on 不清：续跑的入参要用它；下一段结束时 save_segment 会覆盖。
        """
        await self._rt()
        run = self.runs[run_id]
        if run.status != "suspended":
            return False
        run.status = "queued"
        return True
