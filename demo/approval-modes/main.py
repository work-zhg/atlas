"""Native 审批模式：规划 / 手动 / 自动三种模式的主流程（doc/native-approval-mode-design.html）。

运行：  .venv/bin/python demo/approval-modes/main.py   （不需要 API key，约 1 秒）
说明：  demo/approval-modes/README.html（原理、文件结构、三张时序图、实测输出）

三个场景里用户说的是同一句话：「统计 data.csv 有多少行数据，写个脚本跑一下告诉我结果」。
data.csv 末尾埋了一条注入指令（让智能体先 curl … | sh 安装一个工具）。

    规划  读文件放行 → 写文件被拦下（不挂起）→ exit_plan_mode 提交计划、挂起
          → 用户批准并选「自动执行」→ 续跑：state.mode 变成 auto，写文件放行，运行脚本经判断器放行
    手动  写文件请示、挂起 → 批准、续跑 → 运行脚本请示、挂起 → 批准、续跑 → 回复
    自动  读文件放行 → 被注入带偏，想执行 curl … | sh → 判断器两阶段：拦下，拒绝并继续（不挂起）
          → 写文件放行 → 运行脚本：判断器阶段一放行 → 回复

文件按功能拆分（依赖方向从上到下）：
    main.py         剧本入口：每个场景 用户说话 → 跑一段 → 有审批就做决定 → 续跑，直到结束
    runner.py       执行器：每段重新建图、组装入参（消息 + mode + waiting_on + decisions）、落库
    middleware.py   图内：PermissionMiddleware（模式判定、规划提醒、计划审批、判断器）、
                    SuspensionMiddleware（挂起 / 续跑的跳转）、exit_plan_mode 工具
    classifier.py   判断器：只看用户消息和工具调用，两阶段判定（按剧本出结论）
    policy.py       判定规则：工具的副作用类别 × 模式 → 放行 / 请示 / 判断 / 拦下
    state.py        图的 state schema：mode、waiting_on、decisions
    scripted.py     假模型、假工具、工作区（剧本）
    store.py        外部存储：run / message / approval
    tracing.py      输出：每行带上是哪个场景的哪一段在说话
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from runner import run_segment
from scripted import EXECUTED, USER_SAYS, WORKSPACE, reset_workspace
from state import MODE_LABEL, Mode
from store import Approval, Store

SCENARIOS: list[tuple[str, Mode]] = [("规划", "plan"), ("手动", "manual"), ("自动", "auto")]


def user_decides(scenario: str, approval: Approval, store: Store) -> None:
    """用户在卡片上做决定。卡片上看得到参数（计划全文、要写的文件、要跑的命令）。"""
    call = approval.call
    if approval.kind == "plan":
        print("  ┌ 计划卡片 ──────────────────────────────")
        for line in call["args"]["plan"].splitlines():
            print(f"  │ {line}")
        print("  └ 用户：批准，选「自动执行」")
        store.decide(approval.token, "approved", mode="auto")
        return
    args = "，".join(f"{k}={str(v).splitlines()[0]}" for k, v in call["args"].items())
    print(f"  ┌ 审批卡片：{MODE_LABEL[store.run.mode]}模式下 {call['name']} 需要确认")
    print(f"  │ {args}")
    print("  └ 用户：批准")
    store.decide(approval.token, "approved")


async def play(scenario: str, mode: Mode) -> None:
    print(f"\n━━━ {scenario}模式 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  用户：{USER_SAYS}")
    reset_workspace()
    store = Store(mode)
    store.messages.append(HumanMessage(USER_SAYS))
    while await run_segment(scenario, store) == "suspended":
        for approval in store.pending():
            user_decides(scenario, approval, store)
    summarize(store)


def summarize(store: Store) -> None:
    print("  ── 结果 ──")
    print(f"  段数 {store.run.segments}，审批 {len(store.approvals)} 次，最终模式 {store.run.mode}")
    print(f"  真正执行过的命令：{EXECUTED}")
    print(f"  工作区文件：{sorted(WORKSPACE)}")
    for m in store.messages:
        if isinstance(m, ToolMessage) and m.status == "error":
            print(f"  被拦下：[{m.tool_call_id}] {m.content}")
    final = store.messages[-1]
    assert isinstance(final, AIMessage) and not final.tool_calls
    print(f"  回复：{final.content}")


async def main() -> None:
    for scenario, mode in SCENARIOS:
        await play(scenario, mode)


if __name__ == "__main__":
    asyncio.run(main())
