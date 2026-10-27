"""SubAgent：并行派出、父 run 挂起、子 run 跑完叫醒父 run，SubAgent 还能继续派 SubAgent。

运行：  .venv/bin/python demo/subagent/main.py   （不需要 API key，约 1 秒）
说明：  demo/subagent/README.html（原理、文件结构、时序图、动手实验）

场景：用户要选一个消息队列。主智能体分两波派 SubAgent，每一波都是同一批并行发出，每一波都挂起：

    r1 段 1  模型同一批发出 3 个 task → 并行执行：各起一个子 run，在 state.waiting_on 里登记，
             不写结果 → before_model 看到 waiting_on → jump_to end → 消息 + waiting_on 一起落库，
             r1 = suspended，执行它的任务结束
    子 run   r1-c1（调研 Kafka）自己又派了一个 benchmarker r1-c1-c1，于是它也挂起；
             r1-c1-c1 跑完叫醒 r1-c1，r1-c1 续跑、交出结论后再叫醒 r1。
             三个子 run 每结束一个都去叫醒 r1：barrier 不满足就不续，最后一个 CAS 抢到
    r1 段 2  全新的图，入参 = 消息 + waiting_on + results（子 run 的结论）
             → Patch 看到悬空的调用在 waiting_on 里，不补 cancelled
             → before_model 看到结论都到了 → jump_to tools → task 再执行一遍，这次直接返回结论
             → 模型读到三份结论（一份是失败说明），发出第 2 波 → 同样挂起
    r1 段 3  同样续跑 → 回复用户

★ 挂起的方式和 demo/suspend-resume 完全一样：SubAgent 和审批只是 waiting_on 的两种 reason。

文件按功能拆分（依赖方向从上到下）：
    main.py              剧本入口：用户说话 → 提交 run → 等它结束 → 打印各表
    executor.py          执行器：跑一段、落库、barrier、CAS 续跑、组装每段入参
    subagent_service.py  SubAgentSpawner 的实现：task 调用 → 子 run；子 run 的结局 → 结论文本
    agents.py            组装智能体的图（主智能体和 SubAgent 同一套）
    middleware.py        图内：Patch / Suspension / SubAgent 三个中间件、task、SubAgentSpawner
    state.py             图的 state schema：waiting_on（自定义 reducer）、results
    scripted.py          假模型和假工具（剧本）
    store.py             外部存储：thread / run / message 三张表
    tracing.py           输出：每行带上是哪个 run 的哪一段在说话，按嵌套深度缩进
"""

from __future__ import annotations

import asyncio
import time

from executor import Executor
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from scripted import TOOL_SCRIPT, USER_SAYS
from store import TERMINAL, Store


async def until_settled(store: Store, run_id: str, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while store.runs[run_id].status not in TERMINAL:
        if time.monotonic() > deadline:
            print(f"  ⚠ 等了 {timeout:g} 秒，{run_id} 还是 {store.runs[run_id].status}")
            return
        await asyncio.sleep(0.01)


def describe(message: BaseMessage) -> str:
    if isinstance(message, AIMessage) and message.tool_calls:
        calls = "、".join(f"{c['name']}({c['id']})" for c in message.tool_calls)
        return f"{message.type:<6} → {calls}"
    tool_call_id = getattr(message, "tool_call_id", None)
    head = f"{message.type:<6} " + (f"[{tool_call_id}] " if tool_call_id else "")
    return head + str(message.content).split("\n", 1)[0]


def show(store: Store, main_thread_id: str) -> None:
    print("  run 表（每个子 run 在自己的新会话上；id 就是祖先链）")
    for r in sorted(store.runs.values(), key=lambda r: r.id):
        who = store.threads[r.thread_id].subagent_name or "主智能体"
        extra = f"父 run {r.parent_run_id}" if r.parent_run_id else "根 run"
        error = f"（{r.error}）" if r.error else ""
        pad = " " * (12 - sum(2 if ord(c) > 127 else 1 for c in who))  # 中文占两列
        print(f"    {r.id:<10}{who}{pad}{r.status:<10}共 {r.segments} 段  {extra}{error}")
    print("  主会话的 message 表：没有占位消息，也没有 SubAgent 的过程，只有结论")
    for m in store.messages[main_thread_id]:
        print(f"    {describe(m)}")
    child = store.runs["r1-c1"]
    print(f"  子 run {child.id} 的会话：它自己的过程，包括它派出的 benchmarker 和拿回的结论")
    for m in store.messages[child.thread_id]:
        print(f"    {describe(m)}")


async def main() -> None:
    store = Store()
    executor = Executor(store)
    thread = await store.create_thread("主会话")
    print(f"== 用户：「{USER_SAYS}」 ==")
    await store.add_message(thread.id, HumanMessage(USER_SAYS))
    run = await store.create_run(thread.id)

    started = time.monotonic()
    executor.submit(run.id)
    await until_settled(store, run.id)
    serial = sum(duration for duration, _ in TOOL_SCRIPT.values())
    print(
        f"\n整轮用时约 {time.monotonic() - started:.1f} 秒"
        f"（四次工具调用串行要 {serial:.1f} 秒：同一批的子 run 是并行跑的）"
    )

    print("\n== 最终各表 ==")
    show(store, thread.id)


if __name__ == "__main__":
    asyncio.run(main())
