"""ACP v1 → TraceEvent 的翻译表（Bridge 设计 §3 D2；代码设计 §11）。

这个文件就是那张表的可执行形式 —— 每一行输入在这里有一条断言。

验收标准是 **web 一行不改**：所以断言不只看事件类型，还逐字段看 `data`
的形状。前端的 reducer 按字段名取值，少一个字段就是「前端要加分支」，
而那正是这层翻译存在的意义。
"""

from __future__ import annotations

from atlas_server.domain.events import EventType
from atlas_server.host.runtime import note_failed_tool, silent_failure_message
from atlas_server.host.translate.acp_v1 import (
    AcpV1Translator,
    model_of,
    translate_crash,
    translate_stop,
    translate_update,
    usage_of,
)


def _one(payload: dict) -> tuple[EventType, dict]:
    """翻一条 update，断言恰好产出一条事件，返回它。"""
    out = translate_update(payload)
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
        assert translate_update({"sessionUpdate": kind, "content": {"text": ""}}) == []


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
    # ★ 失败原因进 error：前端工具行读的是它
    assert data["error"] == "文件不存在"
    assert "error_kind" not in data


def test_auto_mode_denial_is_recognised() -> None:
    """被 CLI 的 auto 模式拦下（实测的两种措辞）：标成 auto_mode_denied，带上拦截类别。"""
    for text, reason in [
        (
            "Permission denied: [Unverifiable Deletion Target] The user's request to run "
            "`rm -rf /state/*` deletes …",
            "Unverifiable Deletion Target",
        ),
        (
            "Permission for this action was denied by the Claude Code auto mode classifier. "
            "Reason: [Data Exfiltration]. If you have other tasks …",
            "Data Exfiltration",
        ),
    ]:
        kind, data = _one(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-9",
                "status": "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            }
        )
        assert kind is EventType.TOOL_FAILED
        assert data["error_kind"] == "auto_mode_denied"
        assert data["denied_reason"] == reason


def test_an_update_omits_the_fields_it_does_not_carry() -> None:
    """★ 缺席的字段要**省略**，不能写成空串。

    ACP 的 tool_call_update 只带变化的部分 —— title 在 tool_call 时给过就不再
    重复，被拒的工具也没有 output。而前端的 upsertTool 是浅合并，空串会把
    tool_call 时记下的工具名原地抹掉：真机上出现过
    `{"name": "", "result": "", "status": "error"}`，用户在界面上既看不出
    是哪个工具，也看不出为什么失败（doc/detail/suspension.html §12 修正记录 12）。
    """
    _, data = _one({"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "status": "failed"})
    assert data["call_id"] == "tc-1"
    assert data["status"] == "error"
    assert "name" not in data, "没带 title 却写了 name —— 会覆盖掉真的工具名"
    assert "result" not in data, "没有输出却写了 result —— 会覆盖掉真的失败原因"
    assert "result_preview" not in data


def test_in_progress_update_produces_nothing() -> None:
    """中间态不产事件 —— 工具行已由 tool_call 建好，再发只会让它闪烁。"""
    assert (
        translate_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "status": "in_progress"}
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
    update = {"sessionUpdate": "brand_new_kind_from_the_future", "payload": 42}
    assert translate_update(update) == []
    assert update == {"sessionUpdate": "brand_new_kind_from_the_future", "payload": 42}  # 不改输入


def test_missing_discriminator_is_also_survivable() -> None:
    assert translate_update({"content": {"text": "x"}}) == []
    for garbage in (None, "x", 3, [], {"sessionUpdate": 7}):
        assert translate_update(garbage) == []


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
    usage = usage_of({"usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}})
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
    assert translate_update(payload) == translate_update(payload)


# ──────────────────────────────────────────────── 静默失败的措辞


def test_a_single_failure_arriving_twice_is_counted_once() -> None:
    """★ 一次失败会来**两条** tool.failed，不能算成两次。

    平台挡下工具时自己发一条（带名字和原因），CLI 收到 reject 后再发它自己的
    终态 update（按设计不带名字）。当成两次的后果是错误消息里多出一个占位符：
    真机上出现过「……没有给出任何结论：Write /workspace/rejected.txt、?」，
    那个 `?` 一路漏到用户眼前，父模型还专门解释了一句「后面还跟着一个残缺的
    `?`」——凭空给人添一个要排查的东西
    （doc/detail/suspension.html §12 修正记录 12）。
    """
    acc: dict[str, str] = {}
    # 平台那条：带名字和原因
    note_failed_tool(
        acc, {"call_id": "tc-1", "name": "Write /workspace/x.txt", "result": "用户拒绝了这次调用。"}
    )
    # CLI 那条：同一个 call_id，不带名字
    note_failed_tool(acc, {"call_id": "tc-1", "status": "error"})

    assert acc == {"tc-1": "Write /workspace/x.txt"}, "名字被后到的空值覆盖了"
    message = silent_failure_message(acc)
    assert message.endswith("：Write /workspace/x.txt")
    assert "?" not in message


def test_an_unnamed_failure_does_not_invent_a_placeholder() -> None:
    """名字一个都拿不到时，不写名字那一段 —— 而不是编一个 `?`。"""
    acc: dict[str, str] = {}
    note_failed_tool(acc, {"call_id": "tc-9", "status": "error"})

    assert acc == {"tc-9": ""}
    message = silent_failure_message(acc)
    assert message.endswith("。"), message
    assert "?" not in message


def test_distinct_failures_are_all_listed() -> None:
    """真的失败了两个工具时，两个都要说出来。"""
    acc: dict[str, str] = {}
    note_failed_tool(acc, {"call_id": "tc-1", "name": "Write a.txt"})
    note_failed_tool(acc, {"call_id": "tc-2", "name": "Terminal ls"})

    assert silent_failure_message(acc).endswith("：Write a.txt、Terminal ls")


# ──────────────────────────────────────────────── 新形态补充


def test_tool_output_in_the_spec_shape_is_read() -> None:
    """ACP 规范的工具输出是 ``{"type": "content", "content": {ContentBlock}}``。

    旧翻译只认简写形态（内容块顶层带 text），真实适配器的工具输出因此一直是空的。
    """
    _, data = _one(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
        }
    )
    assert data["result"] == "done"


def test_usage_is_normalized_to_the_native_keys() -> None:
    assert usage_of({"usage": {"inputTokens": 3, "thinkingTokens": 2}}) == {
        "input_tokens": 3,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "thinking_tokens": 2,
    }
    assert usage_of({"stopReason": "end_turn"}) is None
    assert usage_of({"usage": "lots"}) is None
    assert usage_of({"usage": {"inputTokens": "3"}})["input_tokens"] == 0  # type: ignore[index]


def test_new_adapter_cache_tokens_are_read() -> None:
    """★ 新 adapter（claude-agent-acp 0.85）的缓存字段叫 cachedReadTokens / cachedWriteTokens。

    数字取自第 0 期实测：adapter 的 input + output + cachedRead == total，即 adapter 的 input
    不含缓存。换算成 native / OTel 的口径后，input 含缓存。
    """
    usage = usage_of(
        {
            "usage": {
                "inputTokens": 19659,
                "outputTokens": 278,
                "cachedReadTokens": 57856,
                "cachedWriteTokens": 0,
                "totalTokens": 77793,
            }
        }
    )
    assert usage is not None
    assert usage["cache_read"] == 57856  # 原先恒为 0
    assert usage["input_tokens"] == 19659 + 57856  # input 含缓存，与 native 同一口径
    assert usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"] == 77793
    # 旧 adapter 的字段名照样认
    assert usage_of({"usage": {"cacheRead": 5, "cacheCreation": 7}})["cache_read"] == 5  # type: ignore[index]


def test_cli_model_is_read_from_quota_meta() -> None:
    response = {
        "stopReason": "end_turn",
        "_meta": {"quota": {"model_usage": [{"model": "deepseek-chat", "token_count": {}}]}},
    }
    assert model_of(response) == "deepseek-chat"
    assert model_of({"stopReason": "end_turn"}) is None
    assert model_of({"_meta": {"quota": {"model_usage": "x"}}}) is None


def test_translator_accumulates_the_answer_in_blocks() -> None:
    """正文按段累积：调工具 = 这一段说完了，下一条 delta 另起一段（与 native 同一判据）。"""
    t = AcpV1Translator()

    def chunk(text: str) -> dict:
        return {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": text}}}

    assert t.update(chunk("我去看看"))[0][1] == {"text": "我去看看", "block": 0}
    t.update({"update": {"sessionUpdate": "tool_call", "toolCallId": "tc", "title": "Read"}})
    assert t.update(chunk("结论"))[0][1]["block"] == 1
    assert t.answer.text == "我去看看结论"


def test_cancelled_turn_with_usage_does_not_crash_and_carries_the_model() -> None:
    """★ 静默 / 断线 / 关闭导致的取消带着用量时，原先 `usage.model_dump()` 会抛 AttributeError。

    （usage 是 dict，不是 pydantic 模型。）
    """
    from atlas_server.host.runtime import _finish

    response = {
        "stopReason": "cancelled",
        "usage": {"inputTokens": 10, "outputTokens": 2, "cachedReadTokens": 30, "totalTokens": 42},
        "_meta": {"quota": {"model_usage": [{"model": "deepseek-chat"}]}},
    }
    out = _finish(
        {"kind": "cancelled", "cause": "idle", "response": response},
        AcpV1Translator(),
        {},
        60.0,
    )
    usage = next(data for kind, data in out if kind is EventType.USAGE_UPDATED)
    assert usage["cache_read"] == 30
    assert usage["input_tokens"] == 40  # 10 + 30：含缓存的口径
    assert usage["model"] == "deepseek-chat"  # 遥测据此计价
    assert out[-1][1]["error_kind"] == "agent_stalled"
