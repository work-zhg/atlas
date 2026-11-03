"""一轮的结果 → 终止事件（c4-model/agent/cli-runtime/code-cli-runtime-turn.html · TurnOutcome）。

bridge 回报的一轮结果：完成、取消（用户要求、静默到点、Pod 终止…）、失败（CLI 报错、进程退出、
取消后没回应…）。完成时交给内容翻译按 CLI 的结束原因出终止事件；取消和失败在这里按原因出。
工具被拦下而 CLI 一个字没说时，给出列着被拦工具名字的错误，而不是一轮空白。
"""

from __future__ import annotations

from typing import Any

from atlas_host import HostErrorCode
from atlas_jsonrpc import RpcFault

from ..domain.events import EventType
from .translate.acp_v1 import (
    AcpV1Translator,
    EventIntent,
    model_of,
    translate_crash,
    translate_stop,
    usage_of,
)

__all__ = [
    "TurnAbort",
    "failed",
    "fault_intent",
    "note_failed_tool",
    "silent_failure_message",
    "terminal_events",
]

#: 没有上报用量时的 usage.updated（token 照样要记账，只是记为 0）
_ZERO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "cache_read": 0,
    "cache_creation": 0,
    "thinking_tokens": 0,
}


class TurnAbort(Exception):
    """一轮没法开始或接着跑：带着要产出的那条 run.failed。"""

    def __init__(self, intent: EventIntent) -> None:
        super().__init__(intent[1].get("message", ""))
        self.intent = intent


def failed(kind: str, message: str) -> EventIntent:
    return EventType.RUN_FAILED, {"error_kind": kind, "message": message, "retryable": True}


def fault_intent(fault: RpcFault) -> EventIntent:
    """bridge 拒绝了请求 → run.failed。"""
    data = fault.data if isinstance(fault.data, dict) else {}
    cause = str(data.get("cause", ""))
    if fault.code == HostErrorCode.AGENT_UNAVAILABLE:
        return failed("runtime_crashed", f"CLI 起不来（{cause}）：{fault.message}")
    if fault.code == HostErrorCode.OPEN_TIMEOUT:
        return failed("runtime_unreachable", f"打开 CLI 会话超时：{fault.message}")
    if fault.code == HostErrorCode.AGENT_ERROR:
        return failed("agent_error", f"CLI 报错：{fault.message}")
    return failed("runtime_unreachable", f"bridge 拒绝了请求（{fault.code} {cause}）")


def terminal_events(
    outcome: dict[str, Any],
    translator: AcpV1Translator,
    failed_tools: dict[str, str],
    timeout: float,
) -> list[EventIntent]:
    """turn.state(ended) 的 outcome → 终止事件意图。"""
    answer = translator.answer
    response = outcome.get("response")
    usage: dict[str, Any] | None = usage_of(response)
    if usage is not None and (model := model_of(response)):
        # 遥测据此给这一轮的总用量计价（RunTrace 的 ACP 合成 Generation）；执行器只取整数字段
        usage = {**usage, "model": model}
    kind = outcome.get("kind")
    if kind == "completed":
        out: list[EventIntent] = [(EventType.MESSAGE_COMPLETED, {"content": answer.content()})]
        # 「工具被挡下且一个字没说」不是成功收尾：CLI 报 end_turn 只表示它讲完了，不表示做成了。
        # 报成成功的话，父智能体拿到一句含糊的「没有产出」，会去排查一个不存在的问题
        if not answer.text.strip() and failed_tools:
            out.append((EventType.USAGE_UPDATED, usage or dict(_ZERO_USAGE)))
            out.append(failed("subagent_silent", silent_failure_message(failed_tools)))
            return out
        for event_kind, data in translate_stop(
            str(outcome.get("stopReason")), usage or dict(_ZERO_USAGE)
        ):
            if event_kind is EventType.RUN_FINISHED:
                data = {**data, "text_len": len(answer.text)}
            out.append((event_kind, data))
        return out
    if kind == "cancelled":
        out = [(EventType.MESSAGE_COMPLETED, {"content": answer.content()})]
        cause = outcome.get("cause")
        if cause == "requested":
            return [*out, *translate_stop("cancelled", usage)]
        if usage is not None:
            # ★ usage 是 dict（usage_of 的返回值），不是 pydantic 模型 —— 原先调 .model_dump()，
            #   一轮因静默 / 断线 / 关闭被取消且带用量时会直接抛 AttributeError
            out.append((EventType.USAGE_UPDATED, dict(usage)))
        if cause == "deadline":
            return [*out, failed("run_timeout", f"acp 一轮超过 {timeout:.0f}s，已取消")]
        if cause == "idle":
            return [*out, failed("agent_stalled", "agent 长时间没有任何进展，已取消这一轮")]
        return [*out, failed("runtime_interrupted", f"这一轮被中断（{cause}）")]
    # failed
    cause = outcome.get("cause")
    error = outcome.get("error") or {}
    if cause == "agent_error":
        acp = error.get("acp") or {}
        message = str(acp.get("message") or error.get("detail") or "agent 返回了错误")
        return [failed("agent_error", f"CLI 报错：{message}")]
    if cause == "cancel_unanswered":
        return translate_crash("取消后 CLI 没有响应，已被终止并重启")
    if cause == "mode_unavailable":
        detail = str(error.get("detail") or "")
        message = f"CLI 无法切换到要求的权限模式，这一轮没有执行。{detail}"
        return [failed("mode_unavailable", message)]
    if cause == "session_ended":
        return translate_crash(f"会话已被 bridge 结束（{outcome.get('detail')}）")
    return translate_crash()


def note_failed_tool(acc: dict[str, str], data: dict[str, Any]) -> None:
    """把一条 tool.failed 记进「这一轮失败了哪些工具」。键是 call_id。

    ★ 按 call_id 收而不是往列表里 append：同一次失败会来**两条** tool.failed ——
      平台挡下时自己发的那条（带名字和原因），和 CLI 随后发的终态 update（不带名字）。
      当成两次失败的后果是错误消息里多出一个占位符：真机上出现过
      「……没有给出任何结论：Write /workspace/x.txt、?」。
    ★ 后到的那条不带名字，不能让它把先到的名字覆盖成空。
    """
    call_id = str(data.get("call_id") or "")
    name = str(data.get("name") or "")
    if name or call_id not in acc:
        acc[call_id] = name or acc.get(call_id, "")


def silent_failure_message(failed_tools: dict[str, str]) -> str:
    """「工具被挡下且一个字没说」的错误文本。只列有名字的，不编占位符。"""
    named = list(dict.fromkeys(name for name in failed_tools.values() if name))
    detail = "：" + "、".join(named) if named else "。"
    return f"子智能体的工具调用被挡下且没有给出任何结论{detail}"
