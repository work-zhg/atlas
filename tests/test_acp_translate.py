"""ACP → TraceEvent 的翻译表（acp 详设 §06）。

这个文件就是那张表的可执行形式 —— 每一行输入在这里有一条断言。

验收标准是 **web 一行不改**：所以断言不只看事件类型，还逐字段看 `data`
的形状。前端的 reducer 按字段名取值，少一个字段就是「前端要加分支」，
而那正是这层翻译存在的意义。
"""

from __future__ import annotations

from atlas_acp.types import AcpUsage
from atlas_acp.updates import parse_update

from atlas_server.acp.translate import translate_crash, translate_stop, translate_update
from atlas_server.domain.events import EventType


def _one(payload: dict) -> tuple[EventType, dict]:
    """翻一条 update，断言恰好产出一条事件，返回它。"""
    out = translate_update(parse_update(payload))
    assert len(out) == 1, f"期望 1 条事件，实际 {len(out)}：{out}"
    return out[0]


# ──────────────────────────────────────────────── 正文与思考


def test_agent_message_chunk_becomes_message_delta() -> None:
    kind, data = _one(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "你好"}}
    )
    assert kind is EventType.MESSAGE_DELTA
    # 与 native 的 _from_messages 同形态：只有 text 一个字段
    assert data == {"text": "你好"}


def test_thought_chunk_does_not_pollute_the_answer() -> None:
    """★ 思考走 thinking.delta，**不是** message.delta。

    message.delta 的 data 只有 {text}，无从区分思考与正文 —— 混进去会被
    拼成 assistant 的最终消息，用户看到一段与回答不连贯的自言自语。
    THINKING_DELTA 本就是为它保留的类型（native 侧因网关不透传思考文本
    而一直空置），前端的事件联合里已有，不需要改一行。
    """
    kind, data = _one(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "先看代码"}}
    )
    assert kind is EventType.THINKING_DELTA
    assert data == {"text": "先看代码"}


def test_empty_chunks_produce_nothing() -> None:
    """只带 signature 的占位块不该变成一串空 delta。"""
    for kind in ("agent_message_chunk", "agent_thought_chunk"):
        assert translate_update(parse_update({"sessionUpdate": kind, "content": {"text": ""}})) == []


# ──────────────────────────────────────────────── 工具调用


def test_tool_call_becomes_tool_started_with_pairing_key() -> None:
    """toolCallId 必须原样带过去 —— 前端靠它把 started 与 completed 合并成一行。"""
    kind, data = _one(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "读取 auth/token.py",
            "kind": "read",
            "rawInput": {"path": "auth/token.py"},
        }
    )
    assert kind is EventType.TOOL_STARTED
    assert data["call_id"] == "tc-1"
    assert data["name"] == "读取 auth/token.py"
    assert data["args"] == {"path": "auth/token.py"}
    # args_preview 复用 native 的 preview_args —— 两条路径的摘要长一样
    assert data["args_preview"] == "path=auth/token.py"
    assert set(data) == {"call_id", "name", "args", "args_preview"}


def test_tool_call_falls_back_to_kind_when_untitled() -> None:
    _, data = _one({"sessionUpdate": "tool_call", "toolCallId": "tc-2", "kind": "execute"})
    assert data["name"] == "execute"


def test_tool_call_update_completed_and_failed() -> None:
    kind, data = _one(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "title": "读取",
            "status": "completed",
            "content": [{"type": "text", "text": "128 行"}],
        }
    )
    assert kind is EventType.TOOL_COMPLETED
    assert data["call_id"] == "tc-1"
    assert data["status"] == "success"
    assert data["result"] == "128 行"
    assert data["result_preview"] == "128 行"

    kind, data = _one(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "failed",
            "content": [{"type": "text", "text": "文件不存在"}],
        }
    )
    assert kind is EventType.TOOL_FAILED
    assert data["status"] == "error"


def test_in_progress_update_produces_nothing() -> None:
    """中间态不产事件 —— 工具行已由 tool_call 建好，再发只会让它闪烁。"""
    assert (
        translate_update(
            parse_update(
                {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "status": "in_progress"}
            )
        )
        == []
    )


def test_long_tool_output_is_previewed_not_dumped() -> None:
    """大结果不进事件流的摘要字段，与 native 同口径（200 字）。"""
    _, data = _one(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "content": [{"type": "text", "text": "x" * 5000}],
        }
    )
    assert len(data["result_preview"]) == 200


# ──────────────────────────────────────────────── 计划


def test_plan_becomes_full_snapshot_todos() -> None:
    """全量快照语义与平台 todos.updated 一致（events.py 契约规则 1）。"""
    kind, data = _one(
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "读代码", "status": "completed"},
                {"content": "改三处", "status": "in_progress"},
                {"content": "跑测试", "status": "pending"},
            ],
        }
    )
    assert kind is EventType.TODOS_UPDATED
    # ACP 的 status 取值与平台 TodoStatus 同名同义，不需要映射表
    assert data == {
        "todos": [
            {"content": "读代码", "status": "completed"},
            {"content": "改三处", "status": "in_progress"},
            {"content": "跑测试", "status": "pending"},
        ]
    }


# ──────────────────────────────────────────────── 未知类型


def test_unknown_update_produces_no_event_and_does_not_raise() -> None:
    """★ 协议演进时旧 server 遇到新 update 类型不该把 run 弄失败。

    翻成 error 的后果：CLI/adapter 升一次版，所有在跑的会话开始报错，
    而实际上什么都没坏。这里只记警告（调用方顺带计数），事件一条不产。
    """
    update = parse_update({"sessionUpdate": "brand_new_kind_from_the_future", "payload": 42})
    assert translate_update(update) == []
    # 原始载荷保留，调用方记日志与指标时能说出「是哪个类型」
    assert update.session_update == "brand_new_kind_from_the_future"
    assert update.raw["payload"] == 42


def test_missing_discriminator_is_also_survivable() -> None:
    assert translate_update(parse_update({"content": {"text": "x"}})) == []


# ──────────────────────────────────────────────── StopReason


def test_end_turn_finishes_the_run() -> None:
    out = translate_stop("end_turn")
    assert [k for k, _ in out] == [EventType.RUN_FINISHED]
    assert out[0][1]["stop_reason"] == "end_turn"


def test_hitting_a_limit_is_finished_not_failed() -> None:
    """★ 撞上限不是失败。

    翻成 run.failed 的话，「任务做完了但被截断」与「真的出错了」在前端
    长得一模一样。stop_reason 作为附加字段带出去 —— 这正是 subagent
    设计里记着要还的那条改进。
    """
    for reason in ("max_tokens", "max_turn_requests"):
        out = translate_stop(reason)
        assert [k for k, _ in out] == [EventType.RUN_FINISHED], reason
        assert out[0][1]["stop_reason"] == reason


def test_refusal_is_a_non_retryable_failure() -> None:
    out = translate_stop("refusal")
    kind, data = out[-1]
    assert kind is EventType.RUN_FAILED
    assert data["error_kind"] == "model_refused"
    assert data["retryable"] is False


def test_cancelled_maps_to_run_cancelled() -> None:
    out = translate_stop("cancelled")
    assert [k for k, _ in out] == [EventType.RUN_CANCELLED]


def test_unknown_stop_reason_still_closes_the_run() -> None:
    """认不出的终了原因照样收尾 —— run 挂在 running 比标错更糟。"""
    out = translate_stop("something_new")
    assert [k for k, _ in out] == [EventType.RUN_FINISHED]


def test_usage_rides_along_before_the_terminal_event() -> None:
    """用量必须排在终止事件**之前** —— SSE 收到终止就关连接（events.py）。"""
    usage = AcpUsage(inputTokens=100, outputTokens=20, totalTokens=120)
    out = translate_stop("end_turn", usage)
    kinds = [k for k, _ in out]
    assert kinds == [EventType.USAGE_UPDATED, EventType.RUN_FINISHED]
    # 键名与 native 的 normalize_usage 一致，前端不需要分支
    assert out[0][1]["input_tokens"] == 100
    assert out[0][1]["total_tokens"] == 120
    assert out[1][1]["total_tokens"] == 120


# ──────────────────────────────────────────────── adapter 崩溃


def test_adapter_crash_is_retryable() -> None:
    """崩溃是故障不是拒答 —— 用户重试一次通常就好了。"""
    kind, data = translate_crash("exit code 137")[0]
    assert kind is EventType.RUN_FAILED
    assert data["error_kind"] == "runtime_crashed"
    assert data["retryable"] is True
    assert "137" in data["message"]


# ──────────────────────────────────────────────── 纯度


def test_translate_is_pure_no_seq_no_clock() -> None:
    """翻译层拿不到事件工厂，就不可能破坏 seq 的单调无空洞（契约规则 2）。

    它返回的是 (类型, data) 意图，seq / run_id / ts 由 _EventFactory 独占。
    同一个输入翻两次，结果完全相同 —— 没有任何隐藏状态。
    """
    payload = {"sessionUpdate": "agent_message_chunk", "content": {"text": "同一句"}}
    assert translate_update(parse_update(payload)) == translate_update(parse_update(payload))
