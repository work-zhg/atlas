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
  LangChain 回调，时间与用量都取自真实调用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import semconv as sc

if TYPE_CHECKING:
    from datetime import datetime

    from ..domain.events import TraceEvent
    from ..executor.assembly import PreparedRun

logger = logging.getLogger(__name__)

__all__ = ["RunTrace"]

#: 纳秒。OTel 的 span 时间戳是 Unix 纳秒整数，而事件带的是 datetime。
_NS = 1_000_000_000


def _ns(ts: datetime) -> int:
    return int(ts.timestamp() * _NS)


class RunTrace:
    """一次 run 的 span 树。用作 context manager。

    用法（executor/inprocess.py::_execute）：

        with RunTrace.start(prepared, run_id=run_id) as rt:
            async for event in runtime.run_turn(...):
                rt.observe(event)
                ...

    关闭时会把还没等到完成事件的子 span 以 ERROR 收尾 —— run 失败或被取消
    时，`tool.started` 可能永远等不到它的 `tool.completed`，不兜底的话那些
    span 会一直挂着，在 Jaeger 上表现为一条永远没结束的调用。
    """

    def __init__(self, root: Any, run_id: str) -> None:
        self._root = root
        self._run_id = run_id
        #: 把根 span 设为「当前 span」的那个上下文管理器，见 __enter__。
        self._current: Any = None
        #: call_id → span。同时承担去重：acp 的一次工具调用会发**两条**
        #: tool.started（tool_call 与随后的 tool_call_update），两条的
        #: call_id 相同 —— 用它作键，第二条自然落回同一个 span。
        self._children: dict[str, Any] = {}

    # ------------------------------------------------------------------ 构造

    @classmethod
    def start(cls, prepared: PreparedRun, *, run_id: str) -> RunTrace:
        from opentelemetry import trace as ot

        from . import tracer

        spec = prepared.spec
        span = tracer().start_span(
            f"invoke_agent {spec.slug}",
            kind=ot.SpanKind.SERVER,
            attributes={
                sc.OPERATION_NAME: sc.OP_INVOKE_AGENT,
                sc.AGENT_NAME: spec.slug,
                sc.REQUEST_MODEL: spec.model.model,
                # §06：这几个维度正是会话管理里已有的主键，不另造
                sc.CONVERSATION_ID: str(prepared.thread_id),
                sc.RUN_ID: run_id,
                sc.AGENT_KIND: spec.kind,
            },
        )
        return cls(span, run_id)

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

        # ★ depth > 0 是子智能体的内部步骤。它有**自己的 run、自己的 trace**
        #   （子会话就是 thread，走同一套执行器），在父这边只记边界。
        #   把它的内部工具也挂进来等于把子任务的过程搬回主 trace，正是
        #   委派设计要避免的那件事。
        if event.depth > 0:
            return

        if kind is EventType.TOOL_STARTED:
            self._open(
                key=str(data.get("call_id") or data.get("name") or ""),
                name=f"execute_tool {data.get('name', '')}".strip(),
                ts=event.ts,
                attributes={
                    sc.OPERATION_NAME: sc.OP_EXECUTE_TOOL,
                    sc.TOOL_NAME: str(data.get("name", "")),
                    sc.TOOL_CALL_ID: str(data.get("call_id", "")),
                },
            )
        elif kind in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
            self._close(
                str(data.get("call_id") or data.get("name") or ""),
                ts=event.ts,
                failed=kind is EventType.TOOL_FAILED,
                message=str(data.get("result_preview") or data.get("status") or ""),
            )
        elif kind is EventType.SUBAGENT_STARTED:
            self._open(
                key=str(data.get("subagent_run_id", "")),
                name=f"invoke_agent {data.get('name', '')}".strip(),
                ts=event.ts,
                attributes={
                    sc.OPERATION_NAME: sc.OP_INVOKE_AGENT,
                    sc.SUBAGENT_NAME: str(data.get("name", "")),
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
            # 用量挂根 span —— 它是**整轮**的累计值（runner 里是累加而非覆盖）
            self._usage(data)
        elif kind is EventType.CONTEXT_COMPACTED:
            # 压缩是长会话质量下降的常见诱因，此前没有观测手段（§05）。
            # 它是瞬时事件，做成 span event 而不是 span。
            self._root.add_event(
                "context.compacted", attributes=_ints(data), timestamp=_ns(event.ts)
            )
        elif kind is EventType.SESSION_LOST:
            self._root.add_event("session.lost", timestamp=_ns(event.ts))
        elif kind in (EventType.RUN_FAILED, EventType.RUN_CANCELLED):
            from opentelemetry.trace import Status, StatusCode

            self._root.set_status(
                Status(StatusCode.ERROR, str(data.get("message") or kind.value))
            )
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

    def _close(self, key: str, *, ts: datetime, failed: bool, message: str) -> None:
        span = self._children.pop(key, None)
        if span is None:
            return
        from opentelemetry.trace import Status, StatusCode

        if failed:
            span.set_status(Status(StatusCode.ERROR, message or "工具调用失败"))
        span.end(end_time=_ns(ts))

    def _usage(self, data: dict) -> None:
        """整轮用量 → 根 span 的 gen_ai.usage.*。

        ★ 缓存与推理 token 必须**单独记**，不能并进 input/output ——
          它们计价不同，合并会让成本核算系统性偏离（§03）。
        """
        mapping = {
            sc.USAGE_INPUT: "input_tokens",
            sc.USAGE_OUTPUT: "output_tokens",
            sc.USAGE_CACHE_READ: "cache_read",
            sc.USAGE_CACHE_CREATION: "cache_creation",
            sc.USAGE_REASONING: "thinking_tokens",
        }
        for attr, source in mapping.items():
            value = data.get(source)
            if isinstance(value, int):
                self._root.set_attribute(attr, value)


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
