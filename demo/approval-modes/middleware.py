"""图内的两个中间件，以及 exit_plan_mode 工具。

    SuspensionMiddleware   before_model：在等审批 → jump_to end（挂起）；
                           等到了 → jump_to tools（续跑）
                           与 demo/suspend-resume 的同名中间件相同
    PermissionMiddleware   awrap_model_call：规划模式下在请求末尾追加提醒（不改系统提示词，不落库）
                           awrap_tool_call：按「模式 × 工具类别」判定 放行 / 请示 / 判断 / 拦下；
                             续跑时按审批结果执行；计划获批时把 state.mode 改成用户选的执行模式

★ 工具列表在三种模式下完全一样（exit_plan_mode 也一直在）：判定发生在调用时，
  切换模式不会改动发给模型的系统提示词和工具定义 —— 那是 prompt 缓存的前缀。
"""

from __future__ import annotations

from typing import Any, Protocol

from classifier import TwoStageClassifier
from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command
from policy import Decision, decide
from scripted import REMINDER_NAME
from state import MODE_LABEL, PermissionState
from tracing import say

PLAN_BLOCKED = "规划模式下不能写文件、执行命令或委派。请先用 exit_plan_mode 提交计划。"
PLAN_REMINDER = (
    "【当前为规划模式】只读调研，不要写文件、执行命令或委派 —— 这些调用会被拦下。"
    "弄清楚现状和需求后，用 exit_plan_mode 提交完整计划，由用户批准并选择执行方式。"
)


class ApprovalBook(Protocol):
    """登记一次审批（server 侧实现，见 store.py）。只登记，不等结果。"""

    def register(self, token: str, kind: str, call: dict[str, Any]) -> None: ...


@tool
def exit_plan_mode(plan: str) -> str:
    """规划模式下完成调研后调用：提交完整计划（目标、调研发现、步骤、验收标准），由用户批准并选择执行方式。"""
    msg = "exit_plan_mode 不会被直接执行：PermissionMiddleware 在 awrap_tool_call 里接管"
    raise RuntimeError(msg)


class SuspensionMiddleware(AgentMiddleware):
    state_schema = PermissionState

    @hook_config(can_jump_to=["tools", "end"])
    def before_model(self, state: PermissionState, runtime: Any = None) -> dict[str, Any] | None:
        waits = state.get("waiting_on") or {}
        if not waits:
            return None
        decisions = state.get("decisions") or {}
        if any(w["token"] in decisions for w in waits.values()):
            say(f"before_model：{list(waits)} 有审批结果了 → jump_to tools")
            return {"jump_to": "tools"}
        say(f"before_model：在等 {list(waits)} → jump_to end（不调模型，本段结束）")
        return {"jump_to": "end"}


class PermissionMiddleware(AgentMiddleware):
    state_schema = PermissionState

    def __init__(self, classifier: TwoStageClassifier, approvals: ApprovalBook) -> None:
        super().__init__()
        self.tools = [exit_plan_mode]
        self._classifier = classifier
        self._approvals = approvals

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        if request.state.get("mode") != "plan":
            return await handler(request)
        reminder = HumanMessage(PLAN_REMINDER, name=REMINDER_NAME)
        return await handler(request.override(messages=[*request.messages, reminder]))

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        call, state = request.tool_call, request.state
        call_id, name = call["id"], call["name"]

        # ① 续跑：这个调用登记过审批，结果已经到了
        wait = (state.get("waiting_on") or {}).get(call_id)
        if wait is not None:
            outcome = state["decisions"][wait["token"]]
            return await self._settle(request, handler, outcome)

        # ② 提交计划：规划模式下登记计划审批；其它模式回错误
        mode = state["mode"]
        if name == exit_plan_mode.name:
            if mode != "plan":
                return _failed(call_id, name, "当前不在规划模式，不需要提交计划。")
            say(f"判定 {call_id} {name}：规划模式下提交计划 → 等用户批准")
            return self._ask(call, kind="plan")

        # ③ 首次见到这个调用：按模式 × 类别判定
        effect, decision = decide(mode, name)
        say(f"判定 {call_id} {name}：{MODE_LABEL[mode]}模式 × {effect.value} → {decision.value}")
        if decision is Decision.ALLOW:
            return await handler(request)
        if decision is Decision.BLOCK:
            return _failed(call_id, name, PLAN_BLOCKED)
        if decision is Decision.ASK:
            return self._ask(call, kind="tool")
        return await self._judge(request, handler)

    def _ask(self, call: dict[str, Any], *, kind: str) -> Command:
        """登记审批，不执行、不写结果，在 waiting_on 里登记一项。

        before_model 看到 waiting_on 非空就让本段结束（挂起）。
        """
        token = f"ap-{call['id']}"
        self._approvals.register(token, kind, call)
        say(f"登记审批 {token}（{'计划' if kind == 'plan' else '工具'}），挂起")
        return Command(update={"waiting_on": {call["id"]: {"reason": "approval", "token": token}}})

    async def _settle(self, request: Any, handler: Any, outcome: dict[str, Any]) -> Command:
        call_id, name = request.tool_call["id"], request.tool_call["name"]
        done: dict[str, Any] = {"waiting_on": {call_id: None}}  # None = 从 waiting_on 里删掉
        if outcome["status"] != "approved":
            reason = outcome.get("feedback") or "用户拒绝了这次操作"
            return Command(update={**done, "messages": [_failed(call_id, name, reason)]})
        if name == exit_plan_mode.name:
            # ★ 唯一的模式转换：用户批准计划，并由用户选执行模式。模型自己没有办法改 mode
            new_mode = outcome["mode"]
            say(f"计划获批 → state.mode：plan → {new_mode}")
            text = f"计划已批准，以{MODE_LABEL[new_mode]}模式执行。按计划开始。"
            message = ToolMessage(text, tool_call_id=call_id, name=name)
            return Command(update={**done, "messages": [message], "mode": new_mode})
        say(f"{call_id} 已批准 → 执行")
        return Command(update={**done, "messages": [await handler(request)]})

    async def _judge(self, request: Any, handler: Any) -> Any:
        call = request.tool_call
        verdict = await self._classifier.classify(call, request.state["messages"])
        if not verdict["block"]:
            say(f"{call['id']} 判断器放行（阶段 {verdict['stage']}）→ 执行")
            return await handler(request)
        # ★ 拒绝并继续（设计 D4）：不挂起，把规则和理由作为工具结果交回，要求换更安全的做法
        say(f"{call['id']} 判断器拦下 → 拒绝并继续（不挂起）")
        text = (
            f"自动模式拦下了这个操作：[{verdict['rule']}] {verdict['reason']}。"
            "不要换工具或换写法达成同一结果；有明显更安全的做法就继续，没有就停下来说明需要用户做什么。"
        )
        return _failed(call["id"], call["name"], text)


def _failed(call_id: str, name: str, text: str) -> ToolMessage:
    return ToolMessage(text, tool_call_id=call_id, name=name, status="error")
