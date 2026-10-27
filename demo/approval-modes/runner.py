"""执行器（server 侧）：组装图、跑一段、落库。

★ 每段都重新建图：模拟「上一段的进程已经退出」，没有任何内存状态可以恢复，
  续跑能依靠的只有存储里的消息、waiting_on、mode，以及审批表里的结果。
"""

from __future__ import annotations

from typing import Any

from classifier import TwoStageClassifier
from langchain.agents import create_agent
from middleware import PermissionMiddleware, SuspensionMiddleware
from scripted import TOOLS, ScriptedModel
from state import MODE_LABEL
from store import Store
from tracing import enter, say


def build_agent(scenario: str, store: Store) -> Any:
    return create_agent(
        model=ScriptedModel(scenario=scenario),
        tools=TOOLS,  # exit_plan_mode 由 PermissionMiddleware 提供
        # 挂起判断在最外层（先于任何为模型调用做的准备），审批判定在里面（同 Atlas 的 build_graph）
        middleware=[SuspensionMiddleware(), PermissionMiddleware(TwoStageClassifier(), store)],
    )


async def run_segment(scenario: str, store: Store) -> str:
    """跑一段，返回落库后的状态：suspended / succeeded。"""
    run = store.run
    run.segments += 1
    run.status = "running"
    enter(scenario, run.segments)
    payload: dict[str, Any] = {"messages": list(store.messages), "mode": run.mode}
    if run.waiting_on:
        payload["waiting_on"] = run.waiting_on
        payload["decisions"] = store.decisions()
        n, waits = len(store.messages), list(run.waiting_on)
        say(f"续跑（全新的图）：消息 {n} 条 + mode={run.mode} + waiting_on {waits}")
    else:
        say(f"开跑：mode={run.mode}（{MODE_LABEL[run.mode]}）")
    result = await build_agent(scenario, store).ainvoke(payload)
    status = store.save_segment(result)
    waits = list(store.run.waiting_on or {})
    n, mode = len(store.messages), store.run.mode
    say(f"落库：消息 {n} 条 + mode={mode} + waiting_on {waits} → {status}")
    return status
