"""智能体的挂起与恢复：最小可运行 demo。

运行：  .venv/bin/python demo/suspend-resume/suspend_resume_state.py   （不需要 API key）
说明：  demo/suspend-resume/README.html（原理、时序图、动手实验）

场景：用户让智能体删一个文件，删除需要人工审批。审批可能要等到第二天，所以分两段跑：

    段 1  模型调用 delete_file → 审批中间件拦下：不执行、不写结果，在 state.waiting_on 里登记
          → before_model 看到 waiting_on 非空 → jump_to "end"（挂起）
          → server 把消息和 waiting_on 一起落库，进程可以退出
    段 2  用户批准 → barrier 满足 → 新建图，入参 = 消息 + waiting_on + decisions
          → 图入口的 PatchToolCallsMiddleware 看到 call_1 在 waiting_on 里，不补 cancelled
          → before_model 看到 call_1 有了决策 → jump_to "tools" → 真正执行删除 → 模型汇报结果

★ 依据有两份：消息表 + run.waiting_on，必须一起存、一起交回。只看消息表，只能看出
  call_1 还没有结果，看不出在等什么（README §8 实验 4 演示了漏传的后果）。

用到的 state schema 知识（详见 demo/state-schema/README.html）：
  自定义 reducer、OmitFromOutput、工具包装钩子返回 Command(update=...)
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any, NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    OmitFromOutput,
    hook_config,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

# ---------------------------------------------------------------------------
# 外部存储。真实系统里是数据库的三张表，进程重启后还在。
# ★ RUN["waiting_on"] 和 MESSAGES 一样是依据，必须一起落库（真实系统里要在同一个事务里）。
# ---------------------------------------------------------------------------
MESSAGES: list[BaseMessage] = []  # message 表
APPROVALS: dict[str, str] = {}  # approval 表：id -> pending / approved / rejected
RUN: dict[str, Any] = {"status": "queued", "waiting_on": None}  # run 表


# ---------------------------------------------------------------------------
# state schema
# ---------------------------------------------------------------------------
def merge_waits(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """waiting_on 的 reducer：按 tool_call_id 合并，值为 None 表示删除。

    为什么不用 operator.add：审批通过后要把这一项**删掉**，add 只能加不能删。
    为什么要 reducer：一批里有多个工具同时挂起时，它们在同一步写 waiting_on，
    没有 reducer 会报 InvalidUpdateError。
    """
    merged = {**(old or {}), **(new or {})}
    return {k: v for k, v in merged.items() if v is not None}


class SuspendState(AgentState):
    # tool_call_id -> {"reason": "approval", "token": "ap-call_1"}
    # 不加可见性标注：挂起时要从返回值里取出来落库，续跑时要原样传回去
    waiting_on: NotRequired[Annotated[dict[str, dict[str, str]], merge_waits]]

    # approval token -> "approved" / "rejected"
    # 只进不出：续跑时由 server 交进来，不需要再带回去
    decisions: NotRequired[Annotated[dict[str, str], OmitFromOutput]]


# ---------------------------------------------------------------------------
# 工具与假模型
# ---------------------------------------------------------------------------
@tool
def delete_file(path: str) -> str:
    """删除文件（危险操作，需要人工审批）。"""
    print(f"    [tool] 执行删除 {path}")  # 示例只打印，不真删
    return f"已删除 {path}"


class ScriptedModel(BaseChatModel):
    """假模型：最后一条是用户消息就调用工具，否则汇报工具结果。"""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        # 模拟真实 API 的配对规则：交给模型的每个 tool_call 都要有且只有一条结果，否则 400
        results = Counter(m.tool_call_id for m in messages if isinstance(m, ToolMessage))
        bad = [
            c["id"]
            for m in messages
            if isinstance(m, AIMessage)
            for c in m.tool_calls
            if results[c["id"]] != 1
        ]
        assert not bad, f"tool_call {bad} 的结果不是恰好一条（真实 API 会报 400）"
        last = messages[-1]
        if isinstance(last, HumanMessage):
            print("    [model] 决定调用 delete_file")
            call = {"name": "delete_file", "args": {"path": "/tmp/demo.txt"}, "id": "call_1"}
            reply = AIMessage("", tool_calls=[call])
        else:
            print("    [model] 汇报结果")
            reply = AIMessage(f"好的，{last.content}。")
        return ChatResult(generations=[ChatGeneration(message=reply)])


# ---------------------------------------------------------------------------
# 三个中间件。都声明 state_schema：合并时重复声明没有副作用，
# 写在每个上面是为了让读代码的人知道「它依赖这些字段」。
# ---------------------------------------------------------------------------
class PatchToolCallsMiddleware(AgentMiddleware):
    """图入口：给被放弃的悬空 tool_call 补一条 cancelled 结果。

    Atlas kernel 里同名中间件的精简版，多了一条判断：登记在 waiting_on 里的调用不补。
    悬空有两种来源，只有后一种该补：
      在等    审批中间件挂起时故意不写结果，并登记在 waiting_on 里
      被放弃  比如进程在工具执行到一半时退出，结果没来得及写
    在等的也补上的话，续跑执行工具后同一个调用就有两份结果（README §6.3）。
    """

    state_schema = SuspendState

    def before_agent(self, state: SuspendState, runtime: Any = None) -> dict[str, Any] | None:
        messages = state["messages"]
        waiting = set(state.get("waiting_on") or {})
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        patched: list[BaseMessage] = []
        for message in messages:
            patched.append(message)
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                if call["id"] in answered:
                    continue
                if call["id"] in waiting:
                    print(f"    [patch] {call['id']} 悬空，但在 waiting_on 里 → 是在等，不补")
                    continue
                print(f"    [patch] {call['id']} 悬空，且不在 waiting_on 里 → 补 cancelled")
                cancelled = f"Tool call {call['name']} with id {call['id']} was cancelled."
                patched.append(ToolMessage(cancelled, tool_call_id=call["id"]))
        if len(patched) == len(messages):
            return None
        # 补的结果要紧跟在对应的 ai 消息后面，所以整体替换
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *patched]}


class ApprovalMiddleware(AgentMiddleware):
    """没批准的调用不执行：只在 state 里登记 waiting_on，不写 tool 消息。"""

    state_schema = SuspendState

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        call_id = request.tool_call["id"]
        token = f"ap-{call_id}"
        decision = (request.state.get("decisions") or {}).get(token)

        if decision == "approved":
            # 真正执行工具，并把这一项从 waiting_on 里删掉（None = 删除）
            return Command(update={"messages": [handler(request)], "waiting_on": {call_id: None}})
        if decision == "rejected":
            rejected = ToolMessage("用户拒绝了这次操作", tool_call_id=call_id)
            return Command(update={"messages": [rejected], "waiting_on": {call_id: None}})

        APPROVALS[token] = "pending"
        print(f"    [approval] 需要审批，登记 waiting_on（approval={token}）")
        # ★ 不写占位 tool 消息，call_1 留着悬空（README §6.2）。配对规则只约束交给模型的消息，
        #   而挂起时 before_model 直接 jump_to end，不调模型；ToolNode 也不检查
        #   wrap_tool_call 的返回值里有没有 ToolMessage（它只检查工具自己返回的 Command）
        return Command(update={"waiting_on": {call_id: {"reason": "approval", "token": token}}})


class SuspensionMiddleware(AgentMiddleware):
    """唯一的图内判断点：只看 state 里的 waiting_on 和 decisions。"""

    state_schema = SuspendState

    @hook_config(can_jump_to=["tools", "end"])
    def before_model(self, state: SuspendState, runtime: Any = None) -> dict[str, Any] | None:
        waits = state.get("waiting_on") or {}
        if not waits:
            return None

        decisions = state.get("decisions") or {}
        released = [cid for cid, w in waits.items() if w["token"] in decisions]
        if released:
            # 等到了：这些调用还悬空着（入口的 Patch 没有补），jump 到 tools 让它们执行
            print(f"    [before_model] {released} 已有决策 → jump_to tools")
            return {"jump_to": "tools"}

        print("    [before_model] waiting_on 非空 → jump_to end（不调模型）")
        return {"jump_to": "end"}


def build_agent() -> Any:
    # 每段都重新建图：模拟「上一段的进程已经退出」，没有任何内存状态可以恢复
    return create_agent(
        model=ScriptedModel(),
        tools=[delete_file],
        middleware=[PatchToolCallsMiddleware(), SuspensionMiddleware(), ApprovalMiddleware()],
    )


# ---------------------------------------------------------------------------
# 「server」：跑一段、落库、判断能否续跑、组装续跑的入参
# ---------------------------------------------------------------------------
def run_segment(payload: dict[str, Any]) -> None:
    result = build_agent().invoke(payload)
    # ★ 两样一起落库：消息，以及 waiting_on（直接取返回值）
    MESSAGES[:] = result["messages"]
    waiting_on = result.get("waiting_on") or None
    RUN.update(status="suspended" if waiting_on else "done", waiting_on=waiting_on)


def waits_are_settled() -> bool:
    """续跑的 barrier：按 reason 判断（README §6.4）。"""
    for wait in (RUN["waiting_on"] or {}).values():
        if wait["reason"] == "approval" and APPROVALS[wait["token"]] == "pending":
            return False
    return True


def resume_payload() -> dict[str, Any]:
    """组装续跑的入参。消息原样交回，**不改写**。

    ★ 三样缺一不可，所以只在这一个函数里组装：
      messages    对话历史
      waiting_on  悬空的 tool_call 在等什么 —— 漏了它，Patch 会把 call_1 当成被放弃、
                  补一句 cancelled，模型照着回答，文件没删（README §8 实验 4）
      decisions   审批结果（从审批表读）。★ 只放已经决策的：before_model 看到 token 在
                  decisions 里就放行，pending 也放进来的话，审批中间件会再登记一次 waiting_on，
                  两边来回跳到 GraphRecursionError（README §8 实验 3′）
    """
    waits = RUN["waiting_on"] or {}
    tokens = [w["token"] for w in waits.values()]
    return {
        "messages": list(MESSAGES),
        "waiting_on": waits,
        "decisions": {t: APPROVALS[t] for t in tokens if APPROVALS[t] != "pending"},
    }


def show(title: str) -> None:
    print(f"\n  {title}  run={RUN}")
    for m in MESSAGES:
        calls = isinstance(m, AIMessage) and m.tool_calls
        extra = f" tool_calls={[c['name'] for c in m.tool_calls]}" if calls else ""
        print(f"    {m.type:>6}: {m.content!r}{extra}")
    print()


def main() -> None:
    MESSAGES.append(HumanMessage("帮我删掉 /tmp/demo.txt"))

    print("== 段 1：首次运行 ==")
    run_segment({"messages": list(MESSAGES)})
    show("消息表")

    print("== 审批还没决策 ==")
    print(f"    barrier 满足吗？{waits_are_settled()}  → 不续跑，run 保持挂起\n")

    print("== 用户点了「批准」 ==")
    token = next(iter(RUN["waiting_on"].values()))["token"]
    APPROVALS[token] = "approved"
    print(f"    barrier 满足吗？{waits_are_settled()}  → 续跑\n")

    print("== 段 2：续跑（全新的图，入参 = 消息 + waiting_on + decisions） ==")
    payload = resume_payload()
    print(f"    入参：waiting_on={payload.get('waiting_on')} decisions={payload['decisions']}")
    run_segment(payload)
    show("消息表")


if __name__ == "__main__":
    main()
