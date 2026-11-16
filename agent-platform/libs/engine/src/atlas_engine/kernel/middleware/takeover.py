"""请人接管浏览器（c4-model/agent/browser/code-browser-takeover.html）。

模型遇到登录、短信码、图形验证码这类自己做不了、也不该做的地方，调 request_takeover 写一句给人看的话
和要打开的地址，然后停下。接管是「等人决定的事项」的一种，走审批同一条路（approval.kind = takeover）：
首次调用时登记、在 state.waiting_on 里登记一项、不写结果，本段随即挂起；人在实时画面里操作完点「交还」
（或「不处理」）后续跑，结果从 state.decisions 取出，换成工具结果交给模型 —— 同一轮继续。不设超时。

    TakeoverAsk         模型的请求：给人看的一句话、要打开的地址（只能是 http(s)）
    takeover_result     决定 → 给模型的工具结果。原生和 CLI 共用这一份（CLI 经 runtime 转出）
    TakeoverMiddleware  提供 request_takeover：登记、挂起，续跑时把结果交给模型

★ 参数里没有会话和窗口：它们由平台带上，模型指定不了别人的会话。
★ 装在 PermissionMiddleware 外层，自己处理自己的工具（同提问）：请人接管在任何审批模式下都允许，
  续跑时这个调用的等待项不能落到 PermissionMiddleware 手里（它会把「没有处理」当成「拒绝执行工具」）。
★ 决定只能由人经接口做出（交还会触发保存登录态和续跑），模型替不了。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError, field_validator

from atlas_engine.contracts import ApprovalGate
from atlas_engine.contracts.tool_names import REQUEST_TAKEOVER
from atlas_engine.kernel.middleware.approval import approval_id_for, run_id_of
from atlas_engine.kernel.middleware.suspension import SuspensionState, release_wait

__all__ = [
    "REQUEST_TAKEOVER",
    "TAKEOVER_DESCRIPTION",
    "TakeoverAsk",
    "TakeoverMiddleware",
    "takeover_result",
]

TAKEOVER_DESCRIPTION = """请用户接管浏览器：遇到要用户本人操作的页面（登录、短信验证码、图形验证码、扫码、支付确认）时调用。调用后这一轮会暂停（可能等很久），用户在对话里的实时画面中自己操作，完成后点「交还」，你会收到结果并接着做。

参数：
- reason：给用户看的一句话：为什么要接管、做完点什么。例：「淘宝要登录才能查订单，请在画面里登录，登录完点交还。」
- url：要在接管窗口里打开的地址（http 或 https），通常是当前页面或登录页。

注意：
- 不要自己尝试输入密码、验证码，也不要让用户把密码发给你。
- 交还后页面可能已经变了：先重新看一眼页面（快照方式 browser snapshot，computer use 方式 browser look），再继续。
- 用户没有处理：不要再为同一件事请求接管，换一种做法，或告诉用户做不了。
- 单独调用这个工具，不要和其它工具放在同一批。"""

HANDED_BACK = (
    "用户已在浏览器里操作完并交还。页面可能已经变了：先重新看一眼页面"
    "（快照方式 browser snapshot，computer use 方式 browser look），再继续。"
)
LOGIN_NOT_SAVED = "这次的登录态没能存下来：下次换了沙箱可能要再登录一次。"
DECLINED = "用户没有接管{why}。不要再为同一件事请求接管：换一种做法，或告诉用户这一步做不了。"
EXPIRED = "接管已失效（这一轮已结束或被取消），用户没有处理。"
NO_GATE = "当前不能请用户接管浏览器（没有人在线处理）：换一种做法，或告诉用户这一步要本人操作。"
GATE_FAILED = (
    "接管请求没有送达用户（审批系统故障，不是用户不处理）：请把这个故障如实告诉用户，"
    "不要改用别的方式绕过登录。"
)


class TakeoverAsk(BaseModel):
    """模型的请求。"""

    reason: str = Field(
        min_length=1,
        max_length=300,
        description="给用户看的一句话：为什么要接管、做完点什么",
    )
    url: str = Field(
        min_length=1, max_length=2048, description="要在接管窗口里打开的地址（http 或 https）"
    )

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("地址只能是 http:// 或 https:// 开头的网址")
        return value.strip()

    @classmethod
    def parse(cls, args: Mapping[str, Any] | None) -> TakeoverAsk:
        return cls.model_validate(dict(args or {}))


def takeover_result(outcome: Mapping[str, Any] | str) -> tuple[str, bool]:
    """决定 → (给模型的工具结果, 是不是错误)。原生和 CLI 共用。

    outcome 是审批的结果（ApprovalOutcome）：status，加上人写的说明（feedback）、登录态存没存下来（login）。
    """
    if isinstance(outcome, str):
        outcome = {"status": outcome}
    status = outcome.get("status")
    note = str(outcome.get("feedback") or "").strip()
    if status == "approved":
        parts = [HANDED_BACK]
        if note:
            parts.append(f"用户的说明：「{note}」")
        login = outcome.get("login")
        if isinstance(login, Mapping) and login.get("saved") is False:
            parts.append(LOGIN_NOT_SAVED)
        return "\n".join(parts), False
    if status == "rejected":
        return DECLINED.format(why=f"，原因：「{note}」" if note else ""), False
    if status == "expired":
        return EXPIRED, True
    # failed 或认不出的值：门禁故障。★ 不能当成 pending 再登记一次（来回跳到 GraphRecursionError）
    return GATE_FAILED, True


def _request_takeover_tool() -> BaseTool:
    """只提供 schema 与描述，函数体永远不会被执行 —— 行为全在 awrap_tool_call 里（同提问）。"""

    def request_takeover(reason: str, url: str) -> str:
        msg = "request_takeover 不会被直接执行：TakeoverMiddleware 在 awrap_tool_call 里接管"
        raise RuntimeError(msg)

    async def arequest_takeover(reason: str, url: str) -> str:
        return request_takeover(reason, url)

    return StructuredTool.from_function(
        name=REQUEST_TAKEOVER,
        func=request_takeover,
        coroutine=arequest_takeover,
        description=TAKEOVER_DESCRIPTION,
        infer_schema=False,
        args_schema=TakeoverAsk,
    )


class TakeoverMiddleware(AgentMiddleware):
    """提供 request_takeover：登记接管、挂起，续跑时把结果交给模型。"""

    state_schema = SuspensionState

    def __init__(self, *, gate: ApprovalGate | None) -> None:
        """gate 为 None（单测、没有门禁）时不挂起：回一条「没有人能处理」—— 不能卡住一轮。"""
        super().__init__()
        self._gate = gate
        self.tools = [_request_takeover_tool()]

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        if call.get("name") != REQUEST_TAKEOVER:
            return await handler(request)
        call_id = str(call.get("id") or "")
        state = getattr(request, "state", None) or {}

        # ① 续跑：这个调用登记过等待项，结果在 decisions 里
        wait = (state.get("waiting_on") or {}).get(call_id)
        if wait is not None:
            token = str(wait.get("token") or "")
            outcome = (state.get("decisions") or {}).get(token)
            if outcome is None:
                # barrier 只在有结果时续跑，走不到这里；真走到了就保持登记，不重复登记
                return Command(update={"waiting_on": {call_id: wait}})
            return release_wait(self._settle(call_id, token, outcome), call_id)

        # ② 首次调用：先校验（模型给的参数不合法就让它改，不登记）
        try:
            ask = TakeoverAsk.parse(call.get("args"))
        except ValidationError as exc:
            return _message(call_id, f"参数不合法，请按说明修正后重新调用：{_brief(exc)}", True)
        if self._gate is None:
            return _message(call_id, NO_GATE, True)
        args = ask.model_dump()
        approval_id = approval_id_for(run_id_of(request), call_id)
        status = await self._gate.check(
            approval_id=approval_id,
            tool_name=REQUEST_TAKEOVER,
            args=args,
            kind="takeover",
            source="browser",
            reason=ask.reason,
            tool_call_id=call_id,
        )
        if status != "pending":
            # 已经有结果（接管后重放到这里）或门禁故障：按结果处理，不再挂起
            return self._settle(call_id, approval_id, {"status": status})

        from langgraph.config import get_stream_writer

        # ★ 事件必须发 —— 前端的接管卡片由它驱动。tool_call_id 让前端把对应的工具行收起来
        get_stream_writer()(
            {
                "kind": "approval.required",
                "approval_id": approval_id,
                "tool_name": REQUEST_TAKEOVER,
                "tool_call_id": call_id,
                "args": args,
                "reason": ask.reason,
                "approval_kind": "takeover",
                "source": "browser",
                "accepts_feedback": True,
            }
        )
        return Command(
            update={"waiting_on": {call_id: {"reason": "approval", "token": approval_id}}}
        )

    def _settle(
        self, call_id: str, approval_id: str, outcome: Mapping[str, Any] | str
    ) -> ToolMessage:
        """按决定生成工具结果，并发 takeover.settled（卡片据此显示结果，刷新后也在）。"""
        from langgraph.config import get_stream_writer

        if isinstance(outcome, str):
            outcome = {"status": outcome}
        text, error = takeover_result(outcome)
        event: dict[str, Any] = {
            "kind": "takeover.settled",
            "approval_id": approval_id,
            "tool_call_id": call_id,
            "decision": outcome.get("status"),
        }
        if note := str(outcome.get("feedback") or "").strip():
            event["note"] = note
        if isinstance(login := outcome.get("login"), Mapping):
            event["login"] = dict(login)
        get_stream_writer()(event)
        return _message(call_id, text, error)


def _brief(exc: ValidationError) -> str:
    """校验错误 → 给模型看的一行（最多三条）。"""
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return "；".join(parts)


def _message(call_id: str, text: str, error: bool) -> ToolMessage:
    return ToolMessage(
        content=text,
        tool_call_id=call_id,
        name=REQUEST_TAKEOVER,
        status="error" if error else "success",
    )
