"""AcpRuntime —— AgentRuntime 的 acp 实现（acp 详设 §09 / §10）。

一轮的编排：

    确保 Pod → 连上 bridge → initialize（探能力位）
      → 确保 CLI 会话（load 恢复 / new 新建，caps 决定）
      → session/prompt
      → update 流经 translate 变成 TraceEvent
      → prompt 响应的 StopReason → 终止事件

★ 与 NativeRuntime 一样：**不落库、不发布、不管锁**。那三件事在执行器的
  前后段，两类 run 共用。唯一的例外是 external_session_id —— 它是会话身份
  不是 run 记录，下一轮恢复全靠它，必须当场落下。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from atlas_acp.caps import AgentCaps
from atlas_acp.types import (
    AcpUsage,
    InitializeParams,
    RequestPermissionResult,
    SessionPromptResult,
)
from atlas_acp.updates import SessionUpdateNotification
from atlas_acp.wire import Method
from sqlalchemy import update as sql_update

from ..db.models import Thread
from ..domain.events import Answer, EventFactory, EventType, TraceEvent
from ..services.approval import RedisApprovalGate
from ..telemetry import semconv as _sc
from ..telemetry import tracer as _tracer
from .channel import AcpChannel, ChannelClosed
from .translate import translate_crash, translate_stop, translate_update

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..config import Settings
    from ..executor.assembly import PreparedRun
    from ..stream.relay import EventRelay
    from .pods import PodProvider

logger = logging.getLogger(__name__)

__all__ = ["AcpRuntime"]

#: 取消轮询间隔。与 native 不同 —— 那边取消由图的 astream 循环顺带检查，
#: 这里要主动问，因为等的是远端的一次长调用。
_CANCEL_POLL_S = 0.5

#: 内部帧：把「有一个审批正在等人应答」送进事件流。
#:
#: ★ 用一个**不可能出现在线协议上**的方法名（atlas/ 前缀）。inbound 队列
#:   里其余都是 bridge 发来的真帧，混进去的这一条必须一眼看得出不是对端
#:   发的 —— 否则下一个读这段代码的人会去 wire.py 里找它。
_APPROVAL_REQUIRED_FRAME = "atlas/internal.approval_required"


class AcpRuntime:
    """进程级构造一次；每轮变化的东西全在 run_turn 的参数里。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        pods: PodProvider,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._pods = pods

    async def run_turn(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
    ) -> AsyncIterator[TraceEvent]:
        events = EventFactory(run_id, _utcnow)
        spec = prepared.spec
        yield events.make(
            EventType.RUN_STARTED,
            {
                "agent_slug": spec.slug,
                "agent_name": spec.name,
                "model": spec.cli.cli_type if spec.cli else "",
                "effort": None,
                "thinking": "none",
                "tools": sorted(spec.tool_names),
            },
        )

        approvals = RedisApprovalGate(
            self._sessionmaker, redis, run_id, timeout_s=self._settings.approval_timeout_s
        )
        inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_notification(frame: dict[str, Any]) -> None:
            await inbound.put(frame)

        async def on_request(frame: dict[str, Any]) -> Any:
            return await self._decide_permission(frame, approvals, inbound)

        if spec.cli is None:
            yield events.make(*_failed("invalid_spec", "acp agent 缺少 cli 配置"))
            return
        try:
            endpoint = await self._pods.ensure(prepared.thread, spec.cli)
        except Exception as exc:  # noqa: BLE001
            yield events.make(*_failed("pod_unavailable", f"会话 Pod 不可用：{exc}"))
            return

        try:
            async with AcpChannel(
                endpoint.url,
                token=endpoint.token,
                thread_id=str(prepared.thread_id),
                on_notification=on_notification,
                on_request=on_request,
                connect_timeout_s=self._settings.acp_ws_connect_timeout_s,
            ) as channel:
                async for event in self._drive(
                    channel, prepared, events=events, relay=relay, run_id=run_id, inbound=inbound
                ):
                    yield event
        except ChannelClosed as exc:
            yield events.make(*_failed("runtime_unreachable", str(exc)))

    # ------------------------------------------------------------------ 一轮

    async def _drive(
        self,
        channel: AcpChannel,
        prepared: PreparedRun,
        *,
        events: EventFactory,
        relay: EventRelay,
        run_id: UUID,
        inbound: asyncio.Queue[dict[str, Any]],
    ) -> AsyncIterator[TraceEvent]:
        timeout = float(prepared.spec.limits.timeout_s)

        # ★ 必须带 protocolVersion —— 真 adapter 会按 schema 校验，少了直接回
        #   -32602 Invalid params，整轮以 runtime_unreachable 失败。
        #   InitializeParams 早就定义好了（types.py），只是这里一直传的是 {}：
        #   假 adapter 完全不看 params，所以进程内测试一路绿灯。
        caps_raw = await channel.request(
            Method.INITIALIZE,
            InitializeParams().model_dump(by_alias=True),
            timeout=timeout,
        )
        caps = AgentCaps(**((caps_raw or {}).get("agentCapabilities") or {}))

        session_id, lost = await self._ensure_session(channel, prepared, caps, timeout=timeout)
        if lost:
            # ★ 恢复失败必须**可见**，不能只写日志（Subagent §04 / §10）。
            yield events.make(
                EventType.SESSION_LOST,
                {"reason": "CLI 会话未能恢复，已新建 —— 上次的上下文不在了"},
            )

        # ★ 发 prompt 之前把队列排空 —— 此刻积压的帧**不属于本轮**。
        #
        #   session/load 按 ACP 规定要先把整段历史以 session/update 重放一遍
        #   再回响应。重放的帧与本轮的 update 走同一个队列，不切这道边界的
        #   后果有两层：
        #     · 事件流里多出上一轮的 thinking 与工具调用，看着像本轮又干了
        #       一遍（而文件时间戳没变、也没产生新的审批）；
        #     · 更糟的是 _pump 会把重放的正文一并累加进 answer，于是落库
        #       的助手消息是「历史 + 本轮」的拼接。表现为用户问一句「文件内容
        #       是什么」，答案里却先把前两轮的回复原样又说了一遍。
        #
        #   判据「此刻队列里的都是旧的」成立，靠的是通知与响应有序 ——
        #   响应到了就意味着重放已经收完。这条顺序由 channel.py 的读循环
        #   保证（它刻意不为通知起任务）。
        dropped = 0
        while not inbound.empty():
            inbound.get_nowait()
            dropped += 1
        if dropped:
            logger.info("丢弃 %d 帧 session/load 重放的历史（不属于本轮）", dropped)

        prompt = asyncio.create_task(
            channel.request(
                Method.SESSION_PROMPT,
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": _text_of(prepared.input_content)}],
                },
                timeout=timeout,
            )
        )

        answer = Answer()
        # ★ 「这一轮在 Pod 那边花了多久」—— 纯 server 侧的观测点：从发出
        #   session/prompt 到拿到响应，中间横跨 WS 往返、bridge 转发、真 CLI
        #   的整轮执行。Pod 内部的细分要等 bridge/CLI 自己接遥测（§05），
        #   本轮不做，但**这一段耗时现在就能看见**，而它正是「改文件卡了
        #   两分钟」这类问题的第一个定位点。
        #
        # ★ 刻意不往线协议里塞 traceparent：ACP 的 _meta 透传与 CLI 是否读
        #   TRACEPARENT 都还是待验证项（§11 第 2 项），而且本轮 Pod 里没有
        #   消费方 —— 加了就是死代码。
        with _tracer().start_as_current_span(
            "acp.session.prompt",
            attributes={
                _sc.OPERATION_NAME: _sc.OP_CHAT,
                _sc.RUN_ID: str(run_id),
                "atlas.acp.session_id": session_id,
            },
        ):
            try:
                async for event in self._pump(
                    inbound, prompt, events=events, relay=relay, run_id=run_id,
                    channel=channel, session_id=session_id, answer=answer,
                ):
                    yield event
                result = SessionPromptResult(**(prompt.result() or {}))
            except ChannelClosed as exc:
                prompt.cancel()
                yield events.make(*translate_crash(str(exc))[0])
                return
            except TimeoutError:
                prompt.cancel()
                yield events.make(*_failed("run_timeout", f"acp 一轮超过 {timeout:.0f}s"))
                return

        yield events.make(EventType.MESSAGE_COMPLETED, {"content": answer.content()})
        for kind, data in translate_stop(result.stop_reason, result.usage or AcpUsage()):
            if kind is EventType.RUN_FINISHED:
                # 全文长度，不分段 —— 这个口径是「这一轮说了多少字」
                data = {**data, "text_len": len(answer.text)}
            yield events.make(kind, data)

    async def _pump(
        self,
        inbound: asyncio.Queue[dict[str, Any]],
        prompt: asyncio.Task[Any],
        *,
        events: EventFactory,
        relay: EventRelay,
        run_id: UUID,
        channel: AcpChannel,
        session_id: str,
        answer: Answer,
    ) -> AsyncIterator[TraceEvent]:
        """把 update 流翻成事件，直到 prompt 有结果。

        ★ 取消要主动问：等的是远端的一次长调用，不像 native 那样有图的
          循环顺带检查。问到之后发 session/cancel，仍然等 prompt 的响应 ——
          CLI 会以 stopReason=cancelled 收尾，那才是取消真正完成的凭据。
        """
        cancel_sent = False
        while True:
            getter = asyncio.create_task(inbound.get())
            done, _ = await asyncio.wait(
                {getter, prompt}, timeout=_CANCEL_POLL_S, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done:
                for event in self._translate(getter.result(), events, answer):
                    yield event
            else:
                getter.cancel()

            if prompt in done:
                # 收尾：把队列里剩下的 update 排干，否则最后几个 token 会丢。
                while not inbound.empty():
                    for event in self._translate(inbound.get_nowait(), events, answer):
                        yield event
                return

            if not cancel_sent and await relay.is_cancelled(run_id):
                cancel_sent = True
                await channel.notify(Method.SESSION_CANCEL, {"sessionId": session_id})

    def _translate(
        self, frame: dict[str, Any], events: EventFactory, answer: Answer
    ) -> list[TraceEvent]:
        method = frame.get("method")
        if method == Method.BRIDGE_ADAPTER_CRASHED:
            params = frame.get("params") or {}
            return [events.make(*translate_crash(str(params.get("reason", "")))[0])]
        if method == _APPROVAL_REQUIRED_FRAME:
            return [events.make(EventType.APPROVAL_REQUIRED, frame.get("params") or {})]
        if method != Method.SESSION_UPDATE:
            return []
        note = SessionUpdateNotification(**(frame.get("params") or {}))
        out: list[TraceEvent] = []
        for kind, data in translate_update(note.parsed()):
            if kind is EventType.MESSAGE_DELTA:
                # 正文累计 —— CLI 不发「完成」信号，message.completed 以
                # prompt 响应返回为界由本 runtime 拼出来（§06）。
                answer.append(str(data.get("text", "")))
                data = {**data, "block": answer.index}
            elif kind is EventType.TOOL_STARTED:
                # ★ 与 native 同一条判据：CLI 调工具 = 这一轮说完了。
                #   真 CLI 同样会先说一句「我来创建这个文件」再调 Write。
                answer.seal()
            out.append(events.make(kind, data))
        return out

    # ------------------------------------------------------------------ 会话

    async def _ensure_session(
        self, channel: AcpChannel, prepared: PreparedRun, caps: AgentCaps, *, timeout: float
    ) -> tuple[str, bool]:
        """返回 (CLI 会话 id, 是否降级为 lost)。"""
        existing = prepared.thread.external_session_id
        if existing and caps.can_resume:
            try:
                await channel.request(
                    Method.SESSION_LOAD,
                    # ★ mcpServers 是**必填**，尽管恢复一个已有会话时它没什么用。
                    #   真 adapter 先按 schema 校验再看代码逻辑，少了就回
                    #   -32602；而调用方捕获后只会降级成 lost，于是表现为
                    #   "每轮都从头开始"，没有任何错误浮到用户面前。
                    #   session/new 一直是带着的，load 漏了 —— 两处必须一致。
                    {"sessionId": existing, "cwd": "/workspace", "mcpServers": []},
                    timeout=timeout,
                )
            except ChannelClosed as exc:
                logger.warning("session/load 失败，降级为新建：%s", exc)
            else:
                return existing, False

        lost = bool(existing)  # 有会话却没能恢复 = 上下文丢了，必须让用户看见
        result = await channel.request(
            Method.SESSION_NEW, {"cwd": "/workspace", "mcpServers": []}, timeout=timeout
        )
        session_id = str((result or {}).get("sessionId", ""))
        await self._remember_session(prepared.thread_id, session_id)
        return session_id, lost

    async def _remember_session(self, thread_id: UUID, session_id: str) -> None:
        """落 external_session_id。

        ★ 这是 runtime 唯一的写库动作，且刻意如此：它是**会话身份**不是
          run 记录 —— 下一轮能不能恢复全靠它，丢了就等于每轮冷启动。
        """
        async with self._sessionmaker() as session:
            await session.execute(
                sql_update(Thread).where(Thread.id == thread_id).values(
                    external_session_id=session_id
                )
            )
            await session.commit()

    # ------------------------------------------------------------------ 审批

    async def _decide_permission(
        self,
        frame: dict[str, Any],
        approvals: RedisApprovalGate,
        inbound: asyncio.Queue[dict[str, Any]],
    ) -> dict[str, Any]:
        """bridge 转来的 request_permission → 平台审批 → 决定回传。

        ★ 与 native 汇入**同一张 Approval 表、同一个前端弹窗**。
        ★ 只映射 allow_once / reject_once —— 「永远允许」是全局策略，
          不能从一次弹窗里溜进来（§07）。
        """
        params = frame.get("params") or {}
        tool_call = params.get("toolCall") or {}
        name = str(tool_call.get("title") or tool_call.get("toolCallId") or "acp_tool")
        # ★ approval_id 现生成，**不复用 toolCallId**：Approval.id 是 UUID，
        #   而 CLI 的 toolCallId 是任意字符串。与 native 的 ApprovalMiddleware
        #   同款做法；toolCallId 留在 args 里给前端配对工具行。
        approval_id = str(uuid4())

        # ★ 先把事件送出去，再去阻塞等决定 —— 顺序反过来就是个死等。
        #   gate.request 会一直等到有人应答（approval_timeout_s 默认 600s），
        #   而前端的弹窗正是由这个事件驱动的（web/src/lib/events.ts 的
        #   ApprovalRequired）。不发的话没有任何人知道有审批在等：CLI 那边
        #   先撞上 bridge 的 300s adapter 超时，整轮以 runtime_crashed 失败，
        #   而 Approval 行还静静躺在库里。
        #
        # ★ 经 inbound 队列绕一圈，而不是就地造事件：_pump 是唯一 yield 事件
        #   的地方，而本回调跑在 channel 的读循环里，没有 yield 的出口。
        #   塞进队列还顺带保证了它与前后 update 的先后关系是对的。
        inbound.put_nowait(
            {
                "method": _APPROVAL_REQUIRED_FRAME,
                # 字段名与 native 逐字对齐（engine 的 ApprovalMiddleware）——
                # 前端 reducer 按字段名取值，少一个就是「前端要加分支」。
                "params": {
                    "approval_id": approval_id,
                    "tool_name": name,
                    "args": tool_call,
                    "reason": "CLI 请求执行该工具的权限",
                },
            }
        )

        decision = await approvals.request(
            approval_id=approval_id, tool_name=name, args=tool_call
        )
        options = params.get("options") or []
        wanted = "allow_once" if decision == "approved" else "reject_once"
        # ★ 回的是**完整响应体** {"outcome": {...}}，不是内层的 outcome 对象。
        #   少包一层的话 CLI 解析不出 optionId，表现为「批准了但 CLI 当成
        #   没选」—— 端到端测试抓到过一次。
        for option in options:
            if option.get("kind") == wanted:
                return RequestPermissionResult.selected(
                    str(option.get("optionId", ""))
                ).model_dump()
        # 没有匹配选项时按取消处理 —— 猜一个 optionId 可能选中
        # 「永远允许」，那是最不该猜错的地方。
        return RequestPermissionResult.cancelled().model_dump()


def _failed(kind: str, message: str) -> tuple[EventType, dict[str, Any]]:
    return EventType.RUN_FAILED, {"error_kind": kind, "message": message, "retryable": True}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return str(content)


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
