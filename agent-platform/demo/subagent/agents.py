"""组装智能体的图。主智能体和 SubAgent 用同一个函数、同一套中间件，区别只在模型的工具：
每个 SubAgent 都装着 task，所以它也能继续派 SubAgent，层数不设上限。

★ 每段都重新建图：模拟「上一段的进程已经退出」，没有任何内存状态可以恢复，
  续跑能依靠的只有数据库里的消息和 waiting_on。
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from middleware import (
    PatchToolCallsMiddleware,
    SubAgentMiddleware,
    SubAgentSpawner,
    SuspensionMiddleware,
)
from scripted import ScriptedModel, run_benchmark, web_search

# SubAgent 名录：名字 -> 干什么的（写进 task 工具的描述，模型据此选人）
SUBAGENTS = {
    "researcher": "调研员，查资料后回一段结论；需要数字时可以再派 benchmarker",
    "benchmarker": "压测员，跑基准测试后回数字",
}
SUBAGENT_TOOLS = {"researcher": web_search, "benchmarker": run_benchmark}


def build_agent(name: str | None, spawner: SubAgentSpawner) -> Any:
    """name 为 None 是主智能体（只有 task），否则是名为 name 的 SubAgent（task + 自己的工具）。"""
    own = [SUBAGENT_TOOLS[name]] if name else []
    return create_agent(
        model=ScriptedModel(tool_name=own[0].name if own else None),
        tools=own,  # task 工具由 SubAgentMiddleware 提供
        middleware=[
            PatchToolCallsMiddleware(),
            SuspensionMiddleware(),
            SubAgentMiddleware(spawner, SUBAGENTS),
        ],
    )
