"""测试用的装配捷径 —— 走真实的 build_graph，不 mock。

engine 的 agent.py 防腐层删除之后，spec → 图的装配住在 server
（executor/build.py）。测试从这里走同一条路径：装配策略被测到的
是真的，不是测试自己拼的一份。
"""

from __future__ import annotations

from typing import Any

from atlas_server.executor.runner import run as engine_run
from atlas_server.executor.build import build_graph

__all__ = ["build_graph", "run_agent"]


def run_agent(
    spec: Any,
    model: Any,
    *,
    run_id: Any,
    input_content: Any,
    history: Any = (),
    cancel: Any = None,
    clock: Any = None,
    titler: Any = None,
    **capabilities: Any,
):
    """spec + model + 能力对象 → TraceEvent 流。

    capabilities 直接透传 build_graph（filesystem / sandbox / approvals /
    compactor / skills / extra_tools / subagents）；cancel / clock / titler
    是 runner 的注入点，随 run() 走。
    """
    graph = build_graph(spec, model, **capabilities)
    return engine_run(
        spec,
        run_id=run_id,
        graph=graph,
        input_content=input_content,
        history=history,
        cancel=cancel,
        clock=clock,
        titler=titler,
    )
