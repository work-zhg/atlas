"""permission.ask → 平台审批 → 答复 / 撤回（Bridge 设计 §4.7；代码设计 §11）。

与 native 汇入**同一张 Approval 表、同一个前端弹窗**。规则（都来自真机上踩过的坑）：
  · 先登记（落库）再发 approval.required —— 事件发出时审批行已在库里，前端刷新核对不会漏
  · 只会选 allow_once；「永远允许」是全局策略，不能从一次弹窗里溜进来
  · 挡下时把**原因**作为 tool.failed 送进事件流 —— CLI 只知道被拒，不知道为什么

等待的上限由 bridge 给出（expiresInS），到点 bridge 自己按拒绝答复 agent 并发 permission.withdraw；
server 收到撤回就把审批行标为 expired，前端的弹窗随之消失。

等决定靠审批组件的决定信号（DecisionChannel），低频回库兜底 —— 原先每秒查一次库，等一天就是
八万多次查询。等待期间 run 标 awaiting_user（状态仍是 running：CLI 和 bridge 都在等）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4

from atlas_engine.kernel.middleware.approval import approval_id_for
from atlas_jsonrpc import current_request_id

from ..domain.events import EventType

__all__ = ["ApprovalPort", "PermissionDesk"]

logger = logging.getLogger(__name__)

#: 挡下的原因 → 给用户看的话
DENIAL_REASON = {
    "rejected": "用户拒绝了这次调用。",
    "expired": "等待人工确认超时，平台按拒绝处理。",
    "failed": "审批系统故障，这次请求**没有任何人看到过** —— 不是用户拒绝。请查服务端日志。",
}

Emit = Callable[[EventType, dict[str, Any]], None]


#: 等决定时多久回库看一眼（决定信号丢了也能返回）
FALLBACK_CHECK_S = 30.0


class ApprovalPort(Protocol):
    """平台审批的最小接口。生产实现是审批组件的 OnlineApprovals。"""

    async def check(
        self,
        *,
        approval_id: str,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> str:
        """首次调用即登记；返回 pending / approved / rejected / expired / failed。"""
        ...

    async def wait(self, approval_id: str, timeout: float) -> str:
        """等这件事有结果，最多 timeout 秒；返回当时的状态（还没结果就是 pending）。"""
        ...

    async def expire(self, approval_id: str) -> None: ...


@dataclass
class _Pending:
    approval_id: str
    withdrawn: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = ""


class PermissionDesk:
    def __init__(
        self,
        approvals: ApprovalPort,
        emit: Emit,
        *,
        run_id: UUID | None = None,
        awaiting: Callable[[], Awaitable[None]] | None = None,
        fallback_s: float = FALLBACK_CHECK_S,
    ) -> None:
        """awaiting：待决的事项变了时调一次，按「还有没有待决的」重算 run 的 awaiting_user。"""
        self._approvals = approvals
        #: 审批 id 由 (run, toolCallId) 派生，和原生一致（kernel 的 approval_id_for）
        self._run_id = run_id
        self._emit = emit
        self._awaiting = awaiting
        self._fallback_s = fallback_s
        #: ask_id（permission.ask 的 JSON-RPC id）→ 等待中的审批
        self._pending: dict[Any, _Pending] = {}
        #: ask_id → approval_id，跨连接保留：断线时处理任务被取消，bridge 重连后以**同一个 id**
        #: 补发这个询问（§7.3）—— 沿用同一条审批，不重复登记、不重复弹窗
        self._approval_ids: dict[Any, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def ask(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理一个 permission.ask，返回 PermissionAnswer 的线上形态。"""
        ask_id = current_request_id()
        request = params.get("request") or {}
        tool_call = request.get("toolCall") or {}
        name = str(tool_call.get("title") or tool_call.get("toolCallId") or "acp_tool")
        call_id = str(tool_call.get("toolCallId") or "") or None
        resent = ask_id in self._approval_ids
        approval_id = self._approval_ids.setdefault(ask_id, self._approval_id(call_id))
        pending = _Pending(approval_id)
        self._pending[ask_id] = pending
        try:
            state = await self._approvals.check(
                approval_id=approval_id, tool_name=name, args=tool_call, tool_call_id=call_id
            )
            await self._sync_awaiting()
            if state == "pending" and not resent:
                # 字段名与 native 逐字对齐 —— 前端 reducer 按字段名取值
                self._emit(
                    EventType.APPROVAL_REQUIRED,
                    {
                        "approval_id": approval_id,
                        "tool_name": name,
                        "args": tool_call,
                        "reason": "CLI 请求执行该工具的权限",
                    },
                )
            if state == "pending":
                state = await self._await_decision(pending)
                await self._sync_awaiting()
        finally:
            self._pending.pop(ask_id, None)

        if state == "withdrawn":
            # bridge 已自行答复 agent（过期按拒绝 / 这一轮已结束），这个响应会被它忽略
            if pending.reason == "expired":
                self._denied(tool_call, name, "expired")
            return {"reject": True}
        if state == "approved":
            option = _option(request, "allow_once")
            if option is not None:
                return {"optionId": option}
            logger.warning("agent 没有提供 allow_once 选项，按拒绝处理")
            return {"reject": True}
        self._denied(tool_call, name, state)
        return {"reject": True}

    def _approval_id(self, call_id: str | None) -> str:
        """★ 由 (run, toolCallId) 派生：bridge 补发同一个请求、换实例接管后重新询问时都落到
        同一行，不会弹出两个窗口（approval 表的唯一键也是这一对）。取不到 toolCallId 才现生成。
        """
        if self._run_id is not None and call_id:
            return approval_id_for(str(self._run_id), call_id)
        return str(uuid4())

    def withdraw(self, ask_id: Any, reason: str) -> None:
        """permission.withdraw：bridge 已经给了 agent 结果，平台这边的审批作废。"""
        pending = self._pending.get(ask_id)
        if pending is None:
            # 处理任务已不在（断线时被取消）：直接作废审批行，前端的弹窗随之消失
            approval_id = self._approval_ids.get(ask_id)
            if approval_id is not None:
                task = asyncio.create_task(self._expire(approval_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return
        pending.reason = reason
        pending.withdrawn.set()

    async def _await_decision(self, pending: _Pending) -> str:
        """等决定（决定信号，fallback_s 回库一次兜底），或被撤回。上限由 bridge 的 expiresInS
        负责，这里不另设 —— 审批只由人决定。"""
        withdrawn = asyncio.ensure_future(pending.withdrawn.wait())
        decided: asyncio.Future[str] | None = None
        try:
            while True:
                decided = asyncio.ensure_future(
                    self._approvals.wait(pending.approval_id, self._fallback_s)
                )
                await asyncio.wait({decided, withdrawn}, return_when=asyncio.FIRST_COMPLETED)
                if withdrawn.done():
                    # 标 expired：前端据此关掉弹窗；用户恰好刚做了决定的话以库里为准
                    with contextlib.suppress(Exception):
                        await self._approvals.expire(pending.approval_id)
                    return "withdrawn"
                try:
                    state = decided.result()
                except Exception:
                    # 查不到（库或 Redis 抖了）：过一会儿再看，不替用户做决定
                    logger.warning("等审批 %s 的决定时出错", pending.approval_id, exc_info=True)
                    await asyncio.sleep(1.0)
                    continue
                if state != "pending":
                    return state
        finally:
            for task in (withdrawn, decided):
                if task is not None and not task.done():
                    task.cancel()

    async def _expire(self, approval_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._approvals.expire(approval_id)
        await self._sync_awaiting()

    async def _sync_awaiting(self) -> None:
        if self._awaiting is None:
            return
        try:
            await self._awaiting()
        except Exception:
            # 只影响界面上的「需要你处理」，不影响审批本身
            logger.warning("run %s 的 awaiting_user 没写上", self._run_id, exc_info=True)

    def _denied(self, tool_call: dict[str, Any], name: str, decision: str) -> None:
        reason = DENIAL_REASON.get(decision, decision)
        self._emit(
            EventType.TOOL_FAILED,
            {
                "call_id": str(tool_call.get("toolCallId") or ""),
                "name": name,
                "status": "error",
                "result": reason,
                "result_preview": reason,
            },
        )


def _option(request: dict[str, Any], kind: str) -> str | None:
    for option in request.get("options") or []:
        if isinstance(option, dict) and option.get("kind") == kind and option.get("optionId"):
            return str(option["optionId"])
    return None
