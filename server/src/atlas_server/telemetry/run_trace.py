"""一次 Run 的 trace —— 根 span + 由事件流派生的子 span（可观测性设计 §04）。

Run 是 trace 的根。不是 Session（太长，一条 trace 装不下），也不是单次
模型调用（太碎，看不到因果）。

★ 子 span 从 **TraceEvent 流**派生，而不是去 runner / kernel 里插桩。
  理由不是省事：native 与 acp 产出**同形事件**是 acp 方案的核心资产
  （tests/test_acp_end_to_end.py::test_acp_turn_produces_native_shaped_events
  钉着这条）。在事件流上派生一次，两条执行路径就都有了 trace，acp 侧
  一行埋点都不用写 —— 而在 runner 里插桩只能覆盖 native。

★ 模型调用（chat）**不在这里**：事件流里没有「模型调用开始/结束」，
  message.delta 只是 token 增量。那条走 model_callback.py 的
  LangChain 回调，时间与用量都取自真实调用。例外是 acp：CLI 内部的调用平台
  看不到，这里用一轮的总用量合成一个 Generation（langfuse-integration-design §6.2）。

★ 归档规则（设计 §5）：
  · trace id = run id 的 128 位整数（由 IdGenerator 给出，见 telemetry.forced_trace_id）
    —— 一个 run 挂起后续跑的多段执行落进**同一条** trace，拿 Atlas 的 run id 就能定位到
    遥测后端的 trace
  · 会话 = **根会话**（子 run 也归到根会话），用户 = 会话所有者
  · 只有 Generation 带 gen_ai.usage.*；根 span 的整轮累计用量改用 atlas.usage.*，
    免得被当成一次模型调用重复计费
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from . import content
from . import semconv as sc

if TYPE_CHECKING:
    from datetime import datetime

    from ..domain.events import TraceEvent
    from ..executor.assembly import PreparedRun

logger = logging.getLogger(__name__)

__all__ = ["RunTrace"]

#: 纳秒。OTel 的 span 时间戳是 Unix 纳秒整数，而事件带的是 datetime。
_NS = 1_000_000_000

#: usage.updated 的键 → (整轮累计的 atlas.* 属性, 单次调用的 gen_ai.* 属性)
#: ★ 缓存与推理 token 必须**单独记**，不能并进 input/output —— 计价不同（§03）。
_USAGE = {
    "input_tokens": (sc.ATLAS_USAGE_INPUT, sc.USAGE_INPUT),
    "output_tokens": (sc.ATLAS_USAGE_OUTPUT, sc.USAGE_OUTPUT),
    "cache_read": (sc.ATLAS_USAGE_CACHE_READ, sc.USAGE_CACHE_READ),
    "cache_creation": (sc.ATLAS_USAGE_CACHE_CREATION, sc.USAGE_CACHE_CREATION),
    "thinking_tokens": (sc.ATLAS_USAGE_REASONING, sc.USAGE_REASONING),
}


def _ns(ts: datetime) -> int:
    return int(ts.timestamp() * _NS)


def _run_trace_id(run_id: str) -> int | None:
    """run id 的 128 位整数 = 这个 run 的 trace id。run_id 不是合法 UUID 时 None（退回随机）。"""
    try:
        return UUID(run_id).int
    except ValueError:
        return None


def _text_of(blocks: Any) -> str:
    """message.completed 的 content blocks → 纯文本。"""
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, list):
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
        kinds = [b.get("type") for b in blocks if isinstance(b, dict)]
        return "".join(str(t) for t, k in zip(texts, kinds, strict=True) if k == "text")
    return ""


class RunTrace:
    """一次 run（的一段执行）的 span 树。用作 context manager。

    用法（executor/inprocess.py::_execute）：

        with RunTrace.start(prepared, run_id=run_id, parent_run_id=...) as rt:
            async for event in runtime.run_turn(...):
                rt.observe(event)
                ...

    关闭时会把还没等到完成事件的子 span 以 ERROR 收尾 —— run 失败或被取消
    时，`tool.started` 可能永远等不到它的 `tool.completed`，不兜底的话那些
    span 会一直挂着，在 Jaeger 上表现为一条永远没结束的调用。
    """

    def __init__(
        self,
        root: Any,
        run_id: str,
        *,
        kind: str = "native",
        base_depth: int = 0,
        model: str = "",
        started_ns: int | None = None,
    ) -> None:
        self._root = root
        self._run_id = run_id
        self._kind = kind
        #: 本 run 在委派树里的深度。比它更深的事件才是「别人的内部步骤」。
        self._base_depth = base_depth
        #: acp 合成 Generation 的回落模型名（CLI 没报模型时用）
        self._model = model
        self._started_ns = started_ns
        #: 把根 span 设为「当前 span」的那个上下文管理器，见 __enter__。
        self._current: Any = None
        #: call_id → span。同时承担去重：acp 的一次工具调用会发**两条**
        #: tool.started（tool_call 与随后的 tool_call_update），两条的
        #: call_id 相同 —— 用它作键，第二条自然落回同一个 span。
        self._children: dict[str, Any] = {}

    # ------------------------------------------------------------------ 构造

    @classmethod
    def start(
        cls, prepared: PreparedRun, *, run_id: str, parent_run_id: str | None = None
    ) -> RunTrace:
        import time

        from opentelemetry import trace as ot

        from . import tracer

        spec = prepared.spec
        thread = getattr(prepared, "thread", None)
        thread_id = str(prepared.thread_id)
        # ★ 会话 = 根会话：子 run 在子会话上，但它是父会话那段对话的一部分（设计 §7）
        session_id = str(getattr(thread, "stream_thread_id", None) or thread_id)
        user_id = getattr(thread, "created_by", None)
        resumed = bool(getattr(prepared, "resume", False))

        attributes: dict[str, Any] = {
            sc.OPERATION_NAME: sc.OP_INVOKE_AGENT,
            sc.AGENT_NAME: spec.slug,
            sc.REQUEST_MODEL: spec.model.model,
            sc.CONVERSATION_ID: session_id,
            sc.RUN_ID: run_id,
            sc.AGENT_KIND: spec.kind,
            sc.THREAD_ID: thread_id,
            sc.RESUMED: resumed,
            # 遥测后端（Langfuse）的归档字段 —— 每一段的根都写，不依赖后端的属性传播
            sc.LF_SESSION_ID: session_id,
            sc.LF_TRACE_NAME: spec.slug,
            sc.LF_TRACE_TAGS: [spec.kind, spec.slug],
            sc.LF_OBSERVATION_TYPE: sc.OBS_AGENT,
            f"{sc.LF_TRACE_METADATA}run_id": run_id,
            f"{sc.LF_TRACE_METADATA}thread_id": thread_id,
        }
        if user_id is not None:
            attributes[sc.LF_USER_ID] = str(user_id)
        if parent_run_id:
            attributes[sc.PARENT_RUN_ID] = parent_run_id
            attributes[f"{sc.LF_TRACE_METADATA}parent_run_id"] = parent_run_id
        environment = _environment()
        if environment:
            attributes[sc.LF_ENVIRONMENT] = environment
        # trace 的输入 = 用户这一轮的消息。续跑的段不重复写（输入早已在第一段里）
        user_input = getattr(prepared, "input_content", None)
        if not resumed and (text := content.render(user_input, "io")) is not None:
            attributes[sc.LF_TRACE_INPUT] = text

        from opentelemetry.context import Context

        from . import forced_trace_id

        started_ns = time.time_ns()
        # ★ 空 Context = 真正的根 span；trace id 由 IdGenerator 按 run id 给出，
        #   于是一个 run 挂起后续跑的每一段都落进同一条 trace
        with forced_trace_id(_run_trace_id(run_id)):
            span = tracer().start_span(
                f"invoke_agent {spec.slug}",
                context=Context(),
                kind=ot.SpanKind.SERVER,
                start_time=started_ns,
                attributes=attributes,
            )
        return cls(
            span,
            run_id,
            kind=spec.kind,
            base_depth=int(getattr(prepared, "base_depth", 0) or 0),
            model=spec.model.model,
            started_ns=started_ns,
        )

    def __enter__(self) -> RunTrace:
        # ★ 必须把根 span 设成**当前 span**，不能只是建出来。
        #
        #   模型调用（model_callback）与审批等待（services/approval）都是靠
        #   「当前上下文里的 span」找父的。不设的话它们全成了各自独立的根 ——
        #   Jaeger 上看到的是一堆平行的 chat span，与 Run 没有任何关系，
        #   而这正是 trace 存在的意义。实测就是这么发现的：工具 span 挂对了
        #   （它显式传 context），chat span 全飘着。
        #
        # ★ end_on_exit=False：根 span 的结束由 close() 负责 —— 它还要先把
        #   残留的子 span 收干净。
        #
        # ★ 不会串到别的 run：contextvars 是按 task 隔离的，而每个 run 跑在
        #   自己的 task 里（submit 还刻意给了全新的 Context）。子 run 同理，
        #   于是它天然是**另一条 trace**，不会被挂进父的树里。
        from opentelemetry import trace as ot

        self._current = ot.use_span(self._root, end_on_exit=False)
        with _quiet():
            self._current.__enter__()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._current is not None:
            with _quiet():
                self._current.__exit__(None, None, None)
            self._current = None
        self.close()

    # ------------------------------------------------------------------ 消费

    def observe(self, event: TraceEvent) -> None:
        """把一条事件反映到 span 树上。

        ★ 整个方法吞异常。遥测坏掉不该让 run 跟着失败 —— 它是旁路，
          而这里跑在 run 的主循环里。
        """
        try:
            self._observe(event)
        except Exception:
            logger.debug("事件转 span 失败：%s", event.type, exc_info=True)

    def _observe(self, event: TraceEvent) -> None:
        from ..domain.events import EventType

        kind = event.type
        data = event.data or {}

        # ★ 比本 run 更深的事件是别人（子智能体）的内部步骤：它有**自己的 run、
        #   自己的 trace**，在这里只记边界。
        # ★ 比较的是 base_depth 而不是 0：子 run 自己的事件 depth 就是 1（EventFactory
        #   叠了 base_depth）。原先写死 `depth > 0`，子 run 的 trace 里因此一个工具、
        #   一条用量都没有 —— acp 子 run 只剩一个空的根 span。
        if event.depth > self._base_depth:
            return

        if kind is EventType.TOOL_STARTED:
            attributes: dict[str, Any] = {
                sc.OPERATION_NAME: sc.OP_EXECUTE_TOOL,
                sc.TOOL_NAME: str(data.get("name", "")),
                sc.TOOL_CALL_ID: str(data.get("call_id", "")),
                sc.LF_OBSERVATION_TYPE: sc.OBS_TOOL,
            }
            args = data.get("args", data.get("args_preview"))
            if (text := content.render(args, "io")) is not None:
                attributes[sc.LF_OBSERVATION_INPUT] = text
            self._open(
                key=str(data.get("call_id") or data.get("name") or ""),
                name=f"execute_tool {data.get('name', '')}".strip(),
                ts=event.ts,
                attributes=attributes,
            )
        elif kind in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            failed = kind is EventType.TOOL_FAILED
            result = data.get("result_preview") or data.get("error") or data.get("result")
            self._close(
                str(data.get("call_id") or data.get("name") or ""),
                ts=event.ts,
                failed=failed,
                message=str(data.get("result_preview") or data.get("status") or ""),
                output=content.render(result, "io"),
            )
            if failed and data.get("error_kind") == "auto_mode_denied":
                # 被 CLI 的 Auto 模式拦下：不是工具坏了，是判断为有风险（acp 权限模式设计 D8-A）
                self._point(
                    "tool.denied",
                    {
                        sc.TOOL_CALL_ID: str(data.get("call_id", "")),
                        "reason": str(data.get("denied_reason", "")),
                    },
                    event.ts,
                )
        elif kind is EventType.SUBAGENT_STARTED:
            self._open(
                key=str(data.get("subagent_run_id", "")),
                name=f"invoke_agent {data.get('name', '')}".strip(),
                ts=event.ts,
                attributes={
                    sc.OPERATION_NAME: sc.OP_INVOKE_AGENT,
                    sc.SUBAGENT_NAME: str(data.get("name", "")),
                    sc.LF_OBSERVATION_TYPE: sc.OBS_AGENT,
                },
            )
        elif kind is EventType.SUBAGENT_FINISHED:
            self._close(
                str(data.get("subagent_run_id", "")),
                ts=event.ts,
                failed=data.get("status") not in (None, "success"),
                message=str(data.get("status") or ""),
            )
        elif kind is EventType.USAGE_UPDATED:
            self._usage(data, ts=event.ts)
        elif kind is EventType.MESSAGE_COMPLETED:
            # trace 的输出 = 最终回答（多段执行时，后一段的覆盖前一段）
            if (text := content.render(_text_of(data.get("content")), "io")) is not None:
                self._root.set_attribute(sc.LF_TRACE_OUTPUT, text)
        elif kind is EventType.AGENT_MODE:
            # acp：这一轮 CLI 实际生效的权限模式（可能被降级）
            self._point(
                "agent.mode",
                {
                    "requested": str(data.get("requested") or ""),
                    "effective": str(data.get("effective") or ""),
                    "degraded": bool(data.get("degraded")),
                },
                event.ts,
            )
        elif kind is EventType.CONTEXT_COMPACTED:
            # 压缩是长会话质量下降的常见诱因，此前没有观测手段（§05）。
            # 它是瞬时事件，做成 span event 而不是 span。
            self._point("context.compacted", _ints(data), event.ts)
        elif kind is EventType.SESSION_LOST:
            self._point("session.lost", {}, event.ts)
        elif kind in (EventType.RUN_FAILED, EventType.RUN_CANCELLED):
            from opentelemetry.trace import Status, StatusCode

            self._root.set_status(Status(StatusCode.ERROR, str(data.get("message") or kind.value)))
            if error_kind := data.get("error_kind"):
                self._root.set_attribute("error.type", str(error_kind))

    # ------------------------------------------------------------------ 收尾

    def close(self) -> None:
        from opentelemetry.trace import Status, StatusCode

        # 残留的子 span：run 失败/取消时会有 started 等不到 completed。
        for key, span in list(self._children.items()):
            with _quiet():
                span.set_status(Status(StatusCode.ERROR, "run 结束时该调用仍未完成"))
                span.end()
            self._children.pop(key, None)
        with _quiet():
            self._root.end()

    # ------------------------------------------------------------------ 内部

    def _open(self, *, key: str, name: str, ts: datetime, attributes: dict) -> None:
        if not key or key in self._children:
            return  # 去重：acp 的 tool_call + tool_call_update 是同一个 call_id
        from opentelemetry import trace as ot

        from . import tracer

        ctx = ot.set_span_in_context(self._root)
        self._children[key] = tracer().start_span(
            name or "execute_tool",
            context=ctx,
            start_time=_ns(ts),
            attributes={**attributes, sc.RUN_ID: self._run_id},
        )

    def _close(
        self, key: str, *, ts: datetime, failed: bool, message: str, output: str | None = None
    ) -> None:
        span = self._children.pop(key, None)
        if span is None:
            return
        from opentelemetry.trace import Status, StatusCode

        if output is not None:
            span.set_attribute(sc.LF_OBSERVATION_OUTPUT, output)
        if failed:
            span.set_status(Status(StatusCode.ERROR, message or "工具调用失败"))
        span.end(end_time=_ns(ts))

    def _point(self, name: str, attributes: dict[str, Any], ts: datetime) -> None:
        """瞬时事件：根 span 上记一条 event（Jaeger），再发一个零时长的 event 类型 span
        （Langfuse 不展示 span event，V8）。"""
        self._root.add_event(name, attributes=attributes, timestamp=_ns(ts))
        from opentelemetry import trace as ot

        from . import tracer

        span = tracer().start_span(
            name,
            context=ot.set_span_in_context(self._root),
            start_time=_ns(ts),
            attributes={
                **attributes,
                sc.LF_OBSERVATION_TYPE: sc.OBS_EVENT,
                sc.RUN_ID: self._run_id,
            },
        )
        span.end(end_time=_ns(ts))

    def _usage(self, data: dict, *, ts: datetime) -> None:
        """整轮用量 → 根 span 的 atlas.usage.*（只给人看）；acp 再合成一个 Generation 计费。

        ★ native 的计费单位是每次模型调用（model_callback 的 chat span 带 gen_ai.usage.*），
          根 span 再带一份 gen_ai.usage.* 就会被算两遍。
        ★ acp 看不到 CLI 内部的单次调用，只能用这一轮的总用量合成**一个** Generation，
          起止时间就是这一轮的起止，并标 atlas.usage.scope = turn。
        """
        for key, (total_attr, _) in _USAGE.items():
            value = data.get(key)
            if isinstance(value, int):
                self._root.set_attribute(total_attr, value)
        if self._kind != "acp":
            return

        from opentelemetry import trace as ot

        from . import tracer

        model = str(data.get("model") or self._model)
        attributes: dict[str, Any] = {
            sc.OPERATION_NAME: sc.OP_CHAT,
            sc.REQUEST_MODEL: model,
            sc.RESPONSE_MODEL: model,
            sc.USAGE_SCOPE: "turn",
            sc.LF_OBSERVATION_TYPE: sc.OBS_GENERATION,
            sc.RUN_ID: self._run_id,
        }
        for key, (_, call_attr) in _USAGE.items():
            value = data.get(key)
            if isinstance(value, int):
                attributes[call_attr] = value
        span = tracer().start_span(
            f"chat {model}".strip(),
            context=ot.set_span_in_context(self._root),
            kind=ot.SpanKind.CLIENT,
            start_time=self._started_ns,
            attributes=attributes,
        )
        span.end(end_time=_ns(ts))


def _environment() -> str | None:
    try:
        from ..config import get_settings

        return get_settings().otel_environment
    except Exception:
        return None


def _ints(data: dict) -> dict[str, int]:
    return {k: v for k, v in data.items() if isinstance(v, int)}


class _quiet:
    """遥测收尾不该抛。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type | None, *_: object) -> bool:
        if exc_type is not None:
            logger.debug("span 收尾失败", exc_info=True)
        return True
