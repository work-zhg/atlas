"""测试用的装配捷径 —— 走真实的 build_graph，不 mock。

engine 的 agent.py 防腐层删除之后，spec → 图的装配住在 server
（executor/build.py）。测试从这里走同一条路径：装配策略被测到的
是真的，不是测试自己拼的一份。
"""

from __future__ import annotations

from typing import Any

from atlas_agent.domain.events import EventType, TraceEvent
from atlas_agent.domain.messages import Transcript
from atlas_agent.executor.build import build_graph
from atlas_agent.executor.runner import run as engine_run
from langchain_core.messages import HumanMessage

__all__ = ["build_graph", "run_agent", "run_to_end"]


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
    transcript: Any = None,
    resume: bool = False,
    base_depth: int = 0,
    start_seq: int = 0,
    waiting_on: Any = None,
    decisions: Any = None,
    **capabilities: Any,
):
    """spec + model + 能力对象 → TraceEvent 流。

    capabilities 直接透传 build_graph（filesystem / sandbox / approvals /
    compactor / skills / extra_tools / subagents）；cancel / clock / titler /
    transcript / resume 是 runner 的注入点，随 run() 走；waiting_on / decisions 是
    续跑时交回图的挂起现场（contracts/suspension.py）。
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
        transcript=transcript,
        resume=resume,
        base_depth=base_depth,
        start_seq=start_seq,
        waiting_on=waiting_on,
        decisions=decisions,
    )


async def run_to_end(
    spec: Any,
    model: Any,
    *,
    run_id: Any,
    input_content: str,
    spawner: Any,
    max_segments: int = 5,
    **kw: Any,
) -> list[TraceEvent]:
    """跑一个会派 SubAgent 的 run 直到结束：挂起了就按执行器的方式续跑。

    每段都重新装图（同一个 model / spawner），续跑的入参与执行器一致：消息原样
    （上一段的 transcript 接在后面）+ waiting_on + decisions（spawner.result 给的结论），
    事件序号接着上一段往下数。返回各段事件拼起来的完整流。
    """
    history: list[Any] = [HumanMessage(content=input_content)]
    events: list[TraceEvent] = []
    waiting_on: dict[str, Any] = {}
    for segment in range(max_segments):
        transcript = Transcript()
        async for event in run_agent(
            spec,
            model,
            run_id=run_id,
            input_content="" if segment else input_content,
            history=history if segment else (),
            resume=bool(segment),
            start_seq=events[-1].seq if events else 0,
            waiting_on=waiting_on or None,
            decisions={w["token"]: spawner.result(w["token"]) for w in waiting_on.values()},
            transcript=transcript,
            spawner=spawner,
            **kw,
        ):
            events.append(event)
        history.extend(transcript.messages)
        if events[-1].type is not EventType.RUN_SUSPENDED:
            return events
        waits = events[-1].data["waiting_on"]
        assert all(w["reason"] == "subagent" for w in waits), waits
        waiting_on = {
            w["tool_call_id"]: {"reason": w["reason"], "token": w["token"]} for w in waits
        }
    raise AssertionError(f"{max_segments} 段之后还在挂起")
