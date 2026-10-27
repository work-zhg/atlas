"""审批模式：按「模式 × 工具的副作用类别」决定放行、请示、拦下（doc/native-approval-mode-design.html）。

取代原先的 ApprovalMiddleware（按作者配置的必审工具名单拦截）。挂起 / 续跑的方式不变：
请示 = 在 state.waiting_on 里登记一项、不写结果，本段随即挂起；审批结果到了续跑时再执行或拒绝
（contracts/suspension.py、doc/detail/suspension.html）。

    判定矩阵          规划    手动    自动
      read            放行    放行    放行
      plan            放行    放行    放行      （exit_plan_mode 见下）
      write           拦下    请示    放行
      execute         拦下    请示    判断
      external        拦下    请示    判断
      delegate        拦下    请示    判断

    拦下  不执行，回一条失败的工具结果说明原因，不挂起
    判断  交给判断器（P2）；P1 还没有判断器：派活放行（子 run 继承模式，动作照样受管），其余请示

★ 工具列表在所有模式下完全一样（exit_plan_mode 也一直在）：判定发生在调用时。切模式不改系统提示词和
  工具定义 —— 它们是 prompt 缓存的前缀。规划模式的说明附在用户本轮那条消息上，只进请求、不进 state。

★ 模式只有一种转换由图内发生：规划模式下 exit_plan_mode 提交计划 → 用户批准并**由用户选**执行模式
  → state.approval_mode 改成那个模式。模型自己没有改模式的工具（防止自己给自己提权）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from typing import Any, Literal, NotRequired

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from atlas_engine.contracts import ApprovalGate, ApprovalMode, ToolEffect
from atlas_engine.kernel.middleware.approval import (
    EXPIRED_RESULT,
    GATE_FAILED_RESULT,
    REJECTED_RESULT,
    approval_id_for,
    run_id_of,
)
from atlas_engine.kernel.middleware.suspension import SuspensionState, release_wait
from atlas_engine.kernel.middleware.tool_search import TOOL_SEARCH

__all__ = [
    "EXIT_PLAN_MODE",
    "MATRIX",
    "PLAN_BLOCKED_RESULT",
    "PLAN_REMINDER",
    "PermissionMiddleware",
    "PermissionState",
    "Verdict",
]

#: 规划模式下提交计划的工具名。沿用 Claude Code 的 ExitPlanMode(plan) 约定 —— 任务书那次的回放实验
#: 表明，字段按 Claude Code 的约定设计后 DeepSeek 用得明显更对
EXIT_PLAN_MODE = "exit_plan_mode"

PLAN_REMINDER = (
    "【当前为规划模式】只读调研，不要写文件、执行命令或委派 —— 这些调用会被拦下。"
    "弄清楚现状和需求后，用 exit_plan_mode 提交完整计划，由用户批准并选择执行方式。"
    "需求不清楚的地方，先问用户（有 ask_user_question 就用它）。"
)
PLAN_BLOCKED_RESULT = (
    "当前是规划模式，不能写文件、执行命令或委派 —— 这个调用没有执行。"
    "请先用 exit_plan_mode 提交计划，用户批准后再执行。"
)
NOT_PLANNING_RESULT = "当前不在规划模式，不需要提交计划，直接执行即可。"

#: 一次判定的结论（设计文档 5.2 的四种结果）
Verdict = Literal["allow", "ask", "judge", "block"]
_ALLOW: Verdict = "allow"
_ASK: Verdict = "ask"
_JUDGE: Verdict = "judge"
_BLOCK: Verdict = "block"

#: 判定矩阵（设计文档 5.2）。改这里之前先改设计文档 —— 它是对用户的承诺
MATRIX: dict[str, dict[ToolEffect, Verdict]] = {
    "plan": {
        ToolEffect.READ: _ALLOW, ToolEffect.PLAN: _ALLOW, ToolEffect.WRITE: _BLOCK,
        ToolEffect.EXECUTE: _BLOCK, ToolEffect.EXTERNAL: _BLOCK, ToolEffect.DELEGATE: _BLOCK,
    },
    "manual": {
        ToolEffect.READ: _ALLOW, ToolEffect.PLAN: _ALLOW, ToolEffect.WRITE: _ASK,
        ToolEffect.EXECUTE: _ASK, ToolEffect.EXTERNAL: _ASK, ToolEffect.DELEGATE: _ASK,
    },
    "auto": {
        ToolEffect.READ: _ALLOW, ToolEffect.PLAN: _ALLOW, ToolEffect.WRITE: _ALLOW,
        ToolEffect.EXECUTE: _JUDGE, ToolEffect.EXTERNAL: _JUDGE, ToolEffect.DELEGATE: _JUDGE,
    },
}  # fmt: skip

#: kernel 自己提供的工具，类别由 kernel 声明（调用方给的映射里可以没有它们）
_KERNEL_TOOLS: dict[str, ToolEffect] = {
    TOOL_SEARCH: ToolEffect.READ,  # 按需加载 MCP 工具的定义
    EXIT_PLAN_MODE: ToolEffect.PLAN,
}

_EFFECT_LABEL = {
    ToolEffect.WRITE: "写入工作区文件",
    ToolEffect.EXECUTE: "执行命令",
    ToolEffect.EXTERNAL: "调用外部工具",
    ToolEffect.DELEGATE: "派活给子智能体",
}
_MODE_LABEL = {"plan": "规划", "manual": "手动", "auto": "自动"}


class PermissionState(SuspensionState):
    """在挂起的两个字段之上多一个 approval_mode。

    ★ 不加可见性标注：开始时由 runner 交进来；计划获批时由本中间件改写；挂起 / 结束时 runner 从最终
      state 里取出来落进 run.approval_mode，续跑时原样交回。
    """

    approval_mode: NotRequired[str]


class _ExitPlanModeInput(BaseModel):
    plan: str = Field(
        description=(
            "完整的计划（Markdown）。用户只看这一段来决定是否批准，必须自足："
            "目标与验收标准；调研发现（读过哪些文件、现状如何）；步骤（每步做什么、改哪些文件）；"
            "要委派的子任务及任务书要点；风险与需要用户决定的事项。"
        )
    )


def _exit_plan_mode_tool() -> BaseTool:
    """只提供 schema 与描述，函数体永远不会被执行 —— 行为全在 awrap_tool_call 里（同 task 工具）。"""

    def exit_plan_mode(plan: str) -> str:
        msg = "exit_plan_mode 不会被直接执行：PermissionMiddleware 在 awrap_tool_call 里接管"
        raise RuntimeError(msg)

    async def aexit_plan_mode(plan: str) -> str:
        return exit_plan_mode(plan)

    return StructuredTool.from_function(
        name=EXIT_PLAN_MODE,
        func=exit_plan_mode,
        coroutine=aexit_plan_mode,
        description=(
            "规划模式下完成调研后调用：提交完整计划，由用户审阅、批准并选择执行方式（手动 / 自动）。"
            "计划开头写「已确认的需求」，包括用户回答过的问题。"
            "用户批准之前不会执行任何有副作用的操作；用户提了修改意见就按意见完善后再次提交。"
            "不在规划模式时不需要调用。"
        ),
        infer_schema=False,
        args_schema=_ExitPlanModeInput,
    )


class PermissionMiddleware(AgentMiddleware):
    """按审批模式决定每次工具调用放行、请示还是拦下；处理规划模式的计划审批。"""

    state_schema = PermissionState

    def __init__(
        self,
        *,
        effects: Mapping[str, ToolEffect],
        default_mode: ApprovalMode,
        gate: ApprovalGate | None,
        execution_modes: tuple[ApprovalMode, ...] = ("manual", "auto"),
        irreversible: Collection[str] = (),
    ) -> None:
        """
        effects          模型侧工具名 → 副作用类别。查不到的按 EXTERNAL（最保守）
        default_mode     state 里没有 approval_mode 时用它（单测、旧数据）
        gate             审批门禁；None 时请示一律按「门禁故障」不放行
        execution_modes  计划获批时用户可选的执行模式（作者允许的范围），随 approval.required 发给前端
        irreversible     不可逆的工具（delete）：自动模式下即使属于工作区写入也不直接放行
        """
        super().__init__()
        self._effects = {**effects, **_KERNEL_TOOLS}
        self._irreversible = frozenset(irreversible)
        self._default_mode = default_mode
        self._gate = gate
        self._execution_modes = execution_modes
        self.tools = [_exit_plan_mode_tool()]

    def _mode(self, state: Mapping[str, Any]) -> str:
        return str(state.get("approval_mode") or self._default_mode)

    # ------------------------------------------------------------------ 模型调用

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        if self._mode(request.state) != "plan":
            return await handler(request)
        return await handler(request.override(messages=_with_plan_reminder(request.messages)))

    # ------------------------------------------------------------------ 工具调用

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or "")
        state = getattr(request, "state", None) or {}

        # ⓪ 图里没有这个工具（模型调了个不存在的）：交给工具节点回「不是有效工具」，
        #   不能为一个不存在的工具请示用户
        if getattr(request, "tool", None) is None:
            return await handler(request)

        # ① 续跑：这个调用登记过等待项
        wait = (state.get("waiting_on") or {}).get(call_id)
        if wait is not None:
            if wait.get("reason") != "approval":
                # 在等的是别的（task 批准后起了子 run，在等子 run 的结论）—— 交给内层，不再请示
                return await handler(request)
            outcome = (state.get("decisions") or {}).get(str(wait.get("token") or ""))
            if outcome is None:
                # barrier 只在有结果时续跑，走不到这里；真走到了就保持登记，不重复登记
                return Command(update={"waiting_on": {call_id: wait}})
            return release_wait(await self._settle(request, handler, outcome), call_id)

        # ② 提交计划
        mode = self._mode(state)
        if name == EXIT_PLAN_MODE:
            if mode != "plan":
                return _failed(call_id, name, NOT_PLANNING_RESULT)
            return await self._ask(request, handler, kind="plan", source="plan_mode",
                                   reason="规划模式：请审阅计划，批准后选择执行方式")  # fmt: skip

        # ③ 首次见到这个调用：查矩阵
        effect = self._effects.get(name, ToolEffect.EXTERNAL)
        verdict = MATRIX[mode][effect]
        if verdict == _ALLOW and mode == "auto" and name in self._irreversible:
            verdict = _JUDGE  # 不可逆的写入（删除）不直接放行（设计 5.2）
        if verdict == _ALLOW:
            return await handler(request)
        if verdict == _BLOCK:
            return _failed(call_id, name, PLAN_BLOCKED_RESULT)
        label = "删除工作区文件" if name in self._irreversible else _EFFECT_LABEL[effect]
        if verdict == _ASK:
            reason = f"手动模式：{label}需要确认"
            return await self._ask(request, handler, kind="tool", source="manual_mode", reason=reason)
        # 判断：P1 还没有判断器（设计文档第 16 节）。
        # ★ 派活放行，其余请示。派活本身没有直接副作用，子 run 继承本 run 的模式，它自己的命令、
        #   外部调用照样要请示；若派活也请示，三种模式下就都无法不经批准派活（规划拦下、手动请示）。
        #   判断器上线后派活改为审查任务书（设计 10.7）
        if effect is ToolEffect.DELEGATE:
            return await handler(request)
        reason = f"自动模式：{label}需要确认（判断器尚未启用）"
        return await self._ask(request, handler, kind="tool", source="auto_uncertain", reason=reason)

    async def _ask(
        self, request: Any, handler: Any, *, kind: str, source: str, reason: str
    ) -> ToolMessage | Command[Any]:
        """登记审批。还没有结果就在 waiting_on 里登记一项、不写结果 —— 本段随即挂起。"""
        from langgraph.config import get_stream_writer

        call = request.tool_call or {}
        call_id, name = str(call.get("id") or ""), str(call.get("name") or "")
        args = call.get("args") or {}
        approval_id = approval_id_for(run_id_of(request), call_id)

        if self._gate is None:
            return _failed(call_id, name, GATE_FAILED_RESULT)
        status = await self._gate.check(
            approval_id=approval_id, tool_name=name, args=args, kind=kind, source=source, reason=reason
        )
        if status != "pending":
            # 已经有结果（接管后重放到这里）或门禁故障：按结果处理，不再挂起
            return await self._settle(request, handler, {"status": status})

        # ★ 事件必须发 —— 前端的审批卡片由它驱动。计划审批另带可选的执行模式
        event: dict[str, Any] = {
            "kind": "approval.required",
            "approval_id": approval_id,
            "tool_name": name,
            "tool_call_id": call_id,
            "args": args,
            "reason": reason,
            "approval_kind": kind,
            "source": source,
            # 拒绝原因 / 修改意见会作为工具结果交给模型。acp 的权限应答带不了原因，不发这一项
            "accepts_feedback": True,
        }
        if kind == "plan":
            event["execution_modes"] = list(self._execution_modes)
        get_stream_writer()(event)
        return Command(
            update={"waiting_on": {call_id: {"reason": "approval", "token": approval_id}}}
        )

    async def _settle(
        self, request: Any, handler: Any, outcome: Mapping[str, Any] | str
    ) -> ToolMessage | Command[Any]:
        """按审批结果执行、拒绝，或（计划获批时）切换模式。"""
        from langgraph.config import get_stream_writer

        call = request.tool_call or {}
        call_id, name = str(call.get("id") or ""), str(call.get("name") or "")
        if isinstance(outcome, str):  # 兜底：旧的字符串形态
            outcome = {"status": outcome}
        status = outcome.get("status")
        feedback = str(outcome.get("feedback") or "").strip()

        if status == "approved":
            if name != EXIT_PLAN_MODE:
                return await handler(request)
            new_mode = str(outcome.get("mode") or "")
            if new_mode not in self._execution_modes:
                return _failed(call_id, name, GATE_FAILED_RESULT)
            # ★ 唯一的图内模式转换。runner 从 agent.mode 事件得知变化，执行器随检查点落库
            get_stream_writer()(
                {
                    "kind": "agent.mode",
                    "requested": new_mode,
                    "effective": new_mode,
                    "degraded": False,
                    "source": "plan_approved",
                }
            )
            text = f"用户已批准计划，选择以{_MODE_LABEL[new_mode]}模式执行。现在按计划开始执行。"
            message = ToolMessage(content=text, tool_call_id=call_id, name=name)
            return Command(update={"messages": [message], "approval_mode": new_mode})

        if status == "rejected":
            if name == EXIT_PLAN_MODE:
                text = (
                    f"用户没有批准这个计划，修改意见：{feedback}。按意见完善后再次调用 exit_plan_mode。"
                    if feedback
                    else "用户没有批准这个计划，也没有给出意见。请先问清楚用户希望怎么调整。"
                )
            else:
                text = (
                    f"用户拒绝执行该工具，原因：{feedback}。请据此换一种方式，或说明为什么必须使用它。"
                    if feedback
                    else REJECTED_RESULT
                )
            return _failed(call_id, name, text)
        if status == "expired":
            return _failed(call_id, name, EXPIRED_RESULT)
        # failed 或认不出的值：门禁故障。★ 不能当成 pending 再登记一次 —— 来回跳到 GraphRecursionError
        return _failed(call_id, name, GATE_FAILED_RESULT)


def _with_plan_reminder(messages: list[Any]) -> list[Any]:
    """把规划提醒附在用户本轮那条消息上（只进请求，不改 state、不落库）。

    ★ 不追加在请求末尾：末尾每次调用都不同，规划期间对话部分的 prompt 缓存会全部失效。附在用户那条
      消息上，规划期间多次调用的前缀保持一致；计划获批切到执行模式时只失效这一次。
    """
    index = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), None
    )
    if index is None:
        return [*messages, HumanMessage(content=PLAN_REMINDER)]
    original = messages[index]
    content = original.content
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    reminded = original.model_copy(
        update={"content": [*blocks, {"type": "text", "text": PLAN_REMINDER}]}
    )
    return [*messages[:index], reminded, *messages[index + 1 :]]


def _failed(call_id: str, name: str, text: str) -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id, name=name, status="error")
