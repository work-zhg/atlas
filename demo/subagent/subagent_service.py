"""SubAgent 的 server 侧：把一次 task 调用变成一个子 run，以及把子 run 的结局整形成结论文本。

图那一侧只认 middleware.SubAgentSpawner 这一个协议；SubAgent 的会话是一行 thread、
子 run 是一行 run，这些只有这里和执行器知道。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from store import TERMINAL, Run, Store
from tracing import say

if TYPE_CHECKING:
    from executor import Executor


class SubAgentService:
    """SubAgentSpawner 的实现。每段建图时新建一个，绑定正在跑的 run（即子 run 的父 run）。"""

    def __init__(self, store: Store, executor: Executor, parent: Run) -> None:
        self.store = store
        self.executor = executor
        self.parent = parent

    async def spawn(self, name: str, task: str, call_id: str) -> str:
        # ★ 子 run 的 id 由 (父 run, tool_call_id) 派生，可复现：这一段被重放时
        #   （上一次落库前进程崩了），同一个调用找到的是同一个子 run，不会重复起（README 实验 6）。
        #   层层派生下去，id 本身就是祖先链：r1-c1-c1 是 r1-c1 派出的
        sub_run_id = f"{self.parent.id}-{call_id}"
        created = await self.store.create_child(
            sub_run_id, parent=self.parent, name=name, task=task
        )
        if not created:
            say(f"{call_id} → 子 run {sub_run_id} 已存在，不重复起")
            return sub_run_id
        say(f"{call_id} → 新建 {name} 的会话和子 run {sub_run_id}，提交执行；登记 waiting_on")
        self.executor.submit(sub_run_id)
        return sub_run_id


async def result_text(store: Store, sub_run_id: str) -> str | None:
    """子 run 的结局 → 交给父 run 模型的工具结果文本。还没结束返回 None。

    ★ 失败也要说清楚：不能让模型把一次失败的 SubAgent 读成「没有发现」。
    """
    run = await store.get_run(sub_run_id)
    if run.status not in TERMINAL:
        return None
    name = store.threads[run.thread_id].subagent_name
    if run.status == "succeeded":
        body = str((await store.history(run.thread_id))[-1].content)
    else:
        body = f"SubAgent {name} 以 failed 结束：{run.error}。没有拿到结论，需要的话重新派"
    return f'{body}\n<subagent name="{name}" run="{sub_run_id}" status="{run.status}"/>'
