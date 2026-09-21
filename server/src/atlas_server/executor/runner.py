"""执行循环：驱动装好的图，产出 TraceEvent 流（文档 §4.5）。

★ 与 build.py 是一对：build 管「图长什么样」，这里管「怎么跑完一轮」。
  刻意保持无基础设施（不碰 DB / Redis / Settings）—— 守卫见
  test_engine_purity.py 的 server 纯层测试。

P4 起内部改为驱动 kernel 的 agent 图（工具调用、待办、虚拟文件系统），
**对外签名与 TraceEvent 契约不变** —— 这正是 §4.2 那层隔离的意义：
内核换了，web 一行不用动。

设计要点：
  · **模型由调用方注入**，engine 不读配置、不碰凭据；单测塞假模型即可
    脱离 DB 与网络运行（§16 P1 的完成标准，P4 之后仍然成立）。
  · 时钟可注入，事件时间戳因此确定。
  · 永不向调用方抛异常 —— 失败也是事件（run.failed），server 的 SSE
    中继只需一条路径。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from atlas_engine.contracts import (
    REFUSAL_STOP_REASONS,
    EngineError,
    LimitExceeded,
    ModelRefused,
    RunTimeout,
    classify,
)
from atlas_engine.kernel.middleware.subagents import SUBAGENT_TOOL
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage

from ..domain.events import Answer, EventFactory, EventType, TraceEvent
from ..domain.spec import AgentSpec
from ..domain.tool_registry import unsupported_tools
from ..domain.translator import (
    extract_text,
    files_delta,
    has_thinking,
    normalize_usage,
    tool_calls_from,
    tool_result_from,
)


class CancelToken(Protocol):
    """取消信号。server 注入 Redis 实现（run:cancel:{run_id}），测试注入假的。"""

    async def is_cancelled(self) -> bool: ...


#: 标题生成器：拿本轮回答文本，返回 thread.title_generated 的 data（§8）
Titler = Callable[[str], Awaitable["dict[str, Any] | None"]]


class _NeverCancelled:
    async def is_cancelled(self) -> bool:
        return False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _graph_input(history: Sequence[BaseMessage], input_content: str | list[dict]) -> dict[str, Any]:
    """system_prompt 交给 agent 自己拼，这里**不再重复塞 SystemMessage**。"""
    messages = list(history)
    messages.append(HumanMessage(content=input_content))  # type: ignore[arg-type]
    return {"messages": messages}


#: 标题生成的兜底上限。真正的 3s 预算在调用方的 titler 内部（文档 §8.2），
#: 这里只是防止一个挂住的 titler 把 run.finished 永远拖着不发。
_TITLE_GUARD_S = 8.0

#: 图内部经 custom stream 通道送出的事件 —— 中间件够不到 runner 的事件工厂
_CUSTOM_EVENTS = {
    "approval.required": EventType.APPROVAL_REQUIRED,
    "context.compacted": EventType.CONTEXT_COMPACTED,
    # 文件不走 graph state 时的补发通道（OssFilesystem 的写路径经此）
    "file.written": EventType.FILE_WRITTEN,
}


async def run(
    spec: AgentSpec,
    *,
    run_id: UUID,
    graph: Any,
    input_content: str | list[dict],
    history: Sequence[BaseMessage] = (),
    cancel: CancelToken | None = None,
    clock: Callable[[], datetime] | None = None,
    titler: Titler | None = None,
) -> AsyncIterator[TraceEvent]:
    """驱动一张**已装配好的图**，产出 TraceEvent 流。

    ★ 图由调用方装配（server/executor/build.py::build_graph）—— runner 只管
      执行循环与事件推导。曾经的 RunHooks 参数对象随图外置一起消失：能力
      对象直接进中间件构造，这里只剩 runner 自己消费的三个注入点
      （cancel / clock / titler）。

    spec 在这里只读三样：run.started 的元信息、timeout_s、max_total_tokens ——
    调用方必须保证 graph 与 spec 出自同一份配置（build.py 是唯一装配点）。
    """
    spec.validate()

    cancel = cancel or _NeverCancelled()
    events = EventFactory(run_id, clock or _utcnow)

    started: dict[str, Any] = {
        "agent_slug": spec.slug,
        "agent_name": spec.name,
        "model": spec.model.model,
        "effort": spec.model.resolve_effort(),
        "thinking": spec.model.resolve_thinking(),
        "tools": sorted(spec.tool_names),
    }
    # 请求了但本期未接的工具要报出来，不能静默忽略（§13.2）
    if missing := unsupported_tools(spec):
        started["unsupported_tools"] = missing
    yield events.make(EventType.RUN_STARTED, started)

    answer = Answer()
    usage: dict[str, int] = {}
    thinking_seen = False
    refused = False
    seen_files: dict[str, str] = {}

    try:
        async with asyncio.timeout(spec.limits.timeout_s):
            # ★ subgraphs=True：子智能体的内部步骤会以带命名空间的形式流出来。
            #   命名空间深度就是 agent 深度 —— 主 agent 是 ()，子智能体是
            #   ('tools:<id>',)。这是 TraceEvent.depth 的唯一来源。
            async for ns, mode, chunk in graph.astream(
                _graph_input(history, input_content),
                # custom 是审批中间件把「需要确认」送出来的通道 ——
                # 它在图内部执行，够不到 runner 的事件工厂（§12.2）。
                stream_mode=["updates", "messages", "custom"],
                subgraphs=True,
            ):
                if await cancel.is_cancelled():
                    yield events.make(
                        EventType.RUN_CANCELLED,
                        {"partial_text_len": len(answer.text)},
                    )
                    return

                depth = len(ns)

                if mode == "messages":
                    for event in _from_messages(chunk, events, answer, depth):
                        yield event
                    msg = chunk[0] if isinstance(chunk, tuple) else chunk
                    if isinstance(msg, AIMessageChunk):
                        # §13.1 model_refused：不是故障，重试无用，
                        # 把模型自己的说明原样展示给用户
                        meta = getattr(msg, "response_metadata", None) or {}
                        if meta.get("stop_reason") in REFUSAL_STOP_REASONS:
                            refused = True
                        thinking_seen = thinking_seen or has_thinking(msg.content)
                        # ★ 累加而非覆盖：一轮对话里模型被调用多次（每次工具
                        #   往返一次），子智能体还会再调。覆盖的话只剩最后一次，
                        #   token 上限形同虚设，账面也对不上。
                        _accumulate(usage, normalize_usage(msg.usage_metadata))
                elif mode == "updates":
                    for event in _from_updates(chunk, events, seen_files, depth, answer):
                        yield event
                elif mode == "custom" and isinstance(chunk, dict):
                    kind = chunk.get("kind")
                    if kind == "usage.delta":
                        # ★ 复合工具内部的模型调用不经过主图，不自报的话
                        #   usage 与 max_total_tokens 对它们全盲
                        #   （docs/research-loop.md §3）。不是事件，只入账。
                        _accumulate(
                            usage,
                            {k: v for k, v in chunk.items() if isinstance(v, int)},
                        )
                    elif kind in _CUSTOM_EVENTS:
                        payload = {k: v for k, v in chunk.items() if k != "kind"}
                        yield events.make(_CUSTOM_EVENTS[kind], payload, depth)

    except TimeoutError:
        yield events.make(
            EventType.RUN_FAILED,
            {
                "error_kind": RunTimeout.kind,
                "message": f"执行超时（{spec.limits.timeout_s}s）",
                "partial_text": answer.text,
            },
        )
        return
    except EngineError as exc:
        yield events.make(
            EventType.RUN_FAILED,
            {
                "error_kind": exc.kind,
                "message": exc.message,
                "retryable": exc.retryable,
                **exc.details,
            },
        )
        return
    except Exception as exc:  # 上游 / 网关异常
        # §13.1：按状态码分类，而不是一律归成 model_unavailable ——
        # 用户看到限流会以为服务挂了，两者的处置完全不同
        classified = classify(exc)
        yield events.make(
            EventType.RUN_FAILED,
            {
                "error_kind": classified.kind,
                "message": classified.message,
                "retryable": classified.retryable,
                **classified.details,
            },
        )
        return

    if usage:
        usage["thinking_occurred"] = thinking_seen  # type: ignore[assignment]
        yield events.make(EventType.USAGE_UPDATED, dict(usage))

    yield events.make(EventType.MESSAGE_COMPLETED, {"content": answer.content()})

    # 下面几处要的是**全文**，不是分段：拒答说明、标题素材、字数统计，
    # 三者都与「模型说了几轮」无关。
    text = answer.text

    if refused:
        yield events.make(
            EventType.RUN_FAILED,
            {
                "error_kind": ModelRefused.kind,
                "message": text or "模型拒绝作答",
                "retryable": False,
            },
        )
        return

    # token 上限是刹车不是硬墙：只能在调用返回后检查（文档 §4.4）
    total = usage.get("total_tokens", 0)
    if total > spec.limits.max_total_tokens:
        yield events.make(
            EventType.RUN_FAILED,
            {
                "error_kind": LimitExceeded.kind,
                "message": f"累计 {total} tokens 超过上限 {spec.limits.max_total_tokens}，已中断",
                "total_tokens": total,
            },
        )
        return

    # §8.1：标题并入 run 的收尾阶段。SSE 在 run.finished 后就关闭了，
    # 异步生成的标题送不出去，所以必须在这之前发完。
    if titler is not None and (title_data := await _generate_title(titler, text)):
        yield events.make(EventType.TITLE_GENERATED, title_data)

    yield events.make(EventType.RUN_FINISHED, {"total_tokens": total, "text_len": len(text)})


async def _generate_title(titler: Titler, text: str) -> dict[str, Any] | None:
    """调用注入的标题生成器。失败一律吞掉 —— 没有标题不该让整个 run 失败。"""
    try:
        async with asyncio.timeout(_TITLE_GUARD_S):
            return await titler(text)
    except Exception:
        # TimeoutError 也在此列（它是 OSError 的子类）。
        # CancelledError 是 BaseException，不会被吞 —— 取消必须继续传播。
        return None


def _accumulate(total: dict[str, int], delta: dict[str, int]) -> None:
    """逐键累加 usage。thinking_occurred 之类的非计数字段由调用方另行处理。"""
    for key, value in delta.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _from_messages(
    chunk: Any, events: EventFactory, answer: Answer, depth: int
) -> list[TraceEvent]:
    """token 级增量 → message.delta。

    ★ 只取 AIMessageChunk 的文本：ToolMessage 也走这条流，
      其 content 是工具返回值，混进正文会让用户看到一堆 JSON。

    ★ depth > 0 的一律丢弃：那是子智能体的内部输出。混进主正文会让用户
      看到一段突然冒出来、与上下文无关的话。子智能体的产出通过 task 工具
      的返回值回到主 agent，最终体现在 subagent.finished 里。
    """
    if depth > 0:
        return []
    msg = chunk[0] if isinstance(chunk, tuple) else chunk
    if not isinstance(msg, AIMessageChunk):
        return []
    delta = extract_text(msg.content)
    if not delta:
        return []
    answer.append(delta)
    # ★ 带上段号：前端要边流边分段，不能等 message.completed 才知道
    #   哪句是过程、哪句是结论。
    return [events.make(EventType.MESSAGE_DELTA, {"text": delta, "block": answer.index})]


def _subagent_started(call: dict[str, Any]) -> dict[str, Any]:
    """task 工具调用 → subagent.started 的 data（文档 §4.2）。"""
    args = call.get("args")
    task = ""
    name = ""
    if isinstance(args, dict):
        task = str(args.get("description", ""))
        name = str(args.get("subagent_type", ""))
    return {
        # 用 tool_call_id 当 subagent_run_id：它同时出现在 task 的调用与返回上，
        # 是唯一能把 started/finished 可靠配对的键（子图命名空间不行，见文档）。
        "subagent_run_id": call.get("call_id", ""),
        "name": name,
        "task": task,
    }


def _from_updates(
    chunk: Any, events: EventFactory, seen_files: dict[str, str], depth: int,
    answer: Answer,
) -> list[TraceEvent]:
    """节点状态更新 → 工具 / 待办 / 文件 / 子智能体事件。"""
    out: list[TraceEvent] = []
    if not isinstance(chunk, dict):
        return out

    for _node, payload in chunk.items():
        if not isinstance(payload, dict):
            continue

        for message in payload.get("messages") or []:
            for call in tool_calls_from(message):
                # ★ 调工具 = 这一轮说完了。主 agent 的话才分段：depth>0 是
                #   子智能体的内部工具，它的文本压根不进主正文。
                if depth == 0:
                    answer.seal()
                # 主 agent 调 task = 委派，不是普通工具调用
                if depth == 0 and call.get("name") == SUBAGENT_TOOL:
                    out.append(
                        events.make(EventType.SUBAGENT_STARTED, _subagent_started(call), depth)
                    )
                else:
                    out.append(events.make(EventType.TOOL_STARTED, call, depth))

            if result := tool_result_from(message):
                failed = result.get("status") == "error"
                if depth == 0 and result.get("name") == SUBAGENT_TOOL:
                    out.append(
                        events.make(
                            EventType.SUBAGENT_FINISHED,
                            {
                                "subagent_run_id": result.get("call_id", ""),
                                "failed": failed,
                                **result,
                            },
                            depth,
                        )
                    )
                else:
                    out.append(
                        events.make(
                            EventType.TOOL_FAILED if failed else EventType.TOOL_COMPLETED,
                            result,
                            depth,
                        )
                    )

        # 契约规则 1：todos 是全量快照，前端不做 diff 合并
        if (todos := payload.get("todos")) is not None:
            out.append(events.make(EventType.TODOS_UPDATED, {"todos": todos}, depth))

        for changed in files_delta(payload.get("files"), seen_files):
            out.append(events.make(EventType.FILE_WRITTEN, changed, depth))

    return out
