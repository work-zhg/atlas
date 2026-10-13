"""LC 消息 ⇄ 存储 blocks 的往返（domain/messages.py）。

这层是分段执行的地基：段与段之间没有别的载体，一轮里模型的 tool_use 与
每个工具的结果都要经它落库再取回。往返一旦不保真，故障不会发生在这里 ——
而是几分钟后另一个 run 向 provider 发出一条格式非法的历史，报错说「消息
格式不对」，根因却在上一个 run 的收尾。

所以这里测的是**不变量**，不是实现细节：
  · tool_use 必然有配对的 tool_result（哪怕一轮是中断的）
  · 一批 tool_result 处在同一个 user turn 里
  · 重建出的 AIMessage 把工具调用放在 tool_calls 字段上，不放在 content 里
  · thinking block 原样留着
"""

from __future__ import annotations

from atlas_server.domain.messages import (
    KIND_CHAT,
    KIND_TOOL_RESULT,
    StoredMessage,
    from_storage,
    to_storage,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _ai_with_call(text: str, call_id: str, name: str = "write_file") -> AIMessage:
    return AIMessage(
        content=[{"type": "text", "text": text}],
        tool_calls=[{"type": "tool_call", "id": call_id, "name": name, "args": {"path": "a.txt"}}],
    )


# --------------------------------------------------------------------------- 出


def test_tool_calls_become_tool_use_blocks_on_the_assistant_turn() -> None:
    """工具调用落在 assistant 的 content 里，是 Anthropic 的原生形状。"""
    stored = to_storage(
        [
            _ai_with_call("我来写这个文件", "toolu_1"),
            ToolMessage(content="ok", tool_call_id="toolu_1"),
        ]
    )

    assert [(s.role, s.kind) for s in stored] == [
        ("assistant", KIND_CHAT),
        ("user", KIND_TOOL_RESULT),
    ]
    assistant = stored[0]
    assert assistant.content[0] == {"type": "text", "text": "我来写这个文件"}
    assert assistant.content[1]["type"] == "tool_use"
    assert assistant.content[1]["id"] == "toolu_1"
    assert assistant.content[1]["input"] == {"path": "a.txt"}


def test_tool_calls_are_synthesized_from_the_field_not_read_from_content() -> None:
    """★ OpenAI 方言下 content 里根本没有 tool_use —— tool_calls 走独立字段。

    从 content 里捞的话，同一个会话换个 provider 就读不回来：落库时丢了
    工具调用，下一轮模型看到的历史里那次调用凭空消失。
    """
    openai_shaped = AIMessage(
        content="我来写这个文件",  # str，不是 blocks
        tool_calls=[{"type": "tool_call", "id": "call_1", "name": "write_file", "args": {}}],
    )
    stored = to_storage([openai_shaped, ToolMessage(content="ok", tool_call_id="call_1")])

    types = [b["type"] for b in stored[0].content]
    assert types == ["text", "tool_use"]


def test_a_batch_of_tool_results_stays_in_one_user_turn() -> None:
    """Anthropic 要求同一批 tool_result 在同一个 user turn 里。

    拆成多条的话模型侧看到的是「工具结果之间插了几轮空对话」，
    而多数 provider 直接报格式错误。
    """
    stored = to_storage(
        [
            AIMessage(
                content=[],
                tool_calls=[
                    {"type": "tool_call", "id": "a", "name": "read_file", "args": {}},
                    {"type": "tool_call", "id": "b", "name": "read_file", "args": {}},
                ],
            ),
            ToolMessage(content="1", tool_call_id="a"),
            ToolMessage(content="2", tool_call_id="b"),
        ]
    )

    assert len(stored) == 2
    assert stored[1].kind == KIND_TOOL_RESULT
    assert [b["tool_use_id"] for b in stored[1].content] == ["a", "b"]


def test_thinking_blocks_survive() -> None:
    """★ 开了 extended thinking 时，带 tool_use 的 assistant turn 要求把
    thinking block 原样回传。丢了轻则降级、重则 400。"""
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "text", "text": "好"},
        ],
        tool_calls=[{"type": "tool_call", "id": "t1", "name": "ls", "args": {}}],
    )
    stored = to_storage([message, ToolMessage(content="ok", tool_call_id="t1")])

    thinking = [b for b in stored[0].content if b["type"] == "thinking"]
    assert thinking == [{"type": "thinking", "thinking": "", "signature": "sig-abc"}]


def test_error_status_becomes_is_error() -> None:
    stored = to_storage(
        [
            _ai_with_call("试试", "t1"),
            ToolMessage(content="boom", tool_call_id="t1", status="error"),
        ]
    )
    assert stored[1].content[0]["is_error"] is True


def test_system_and_remove_messages_are_not_stored() -> None:
    """system 每轮由 spec 重新拼（build.py 是唯一装配点），落库会让它出现两份。"""
    from langchain_core.messages import RemoveMessage, SystemMessage

    stored = to_storage(
        [SystemMessage(content="你是助手"), RemoveMessage(id="x"), HumanMessage(content="在吗")]
    )
    assert [(s.role, s.kind) for s in stored] == [("user", KIND_CHAT)]


# --------------------------------------------------------------------------- 补缺


def test_a_tool_call_with_no_result_gets_one_synthesized() -> None:
    """★ 一轮在工具返回前就断了（网关挂了 / 进程被杀）。

    不补的话下一轮带着这段历史直接 400，而报错指向「消息格式」——
    根因却在上一个 run 的收尾。
    """
    stored = to_storage([_ai_with_call("我来写", "toolu_1")])

    assert len(stored) == 2, "缺的那条 tool_result 必须被补上"
    assert stored[1].kind == KIND_TOOL_RESULT
    assert stored[1].content[0]["tool_use_id"] == "toolu_1"
    assert stored[1].content[0]["is_error"] is True


def test_partial_results_are_topped_up_in_place() -> None:
    """两个调用只回来一个 —— 补进**已有的那一批**，不另起一条 user turn。"""
    stored = to_storage(
        [
            AIMessage(
                content=[],
                tool_calls=[
                    {"type": "tool_call", "id": "a", "name": "ls", "args": {}},
                    {"type": "tool_call", "id": "b", "name": "ls", "args": {}},
                ],
            ),
            ToolMessage(content="1", tool_call_id="a"),
        ]
    )

    assert len(stored) == 2
    assert [b["tool_use_id"] for b in stored[1].content] == ["a", "b"]
    assert stored[1].content[0]["is_error"] is False
    assert stored[1].content[1]["is_error"] is True


def test_every_tool_use_has_a_result_after_a_multi_round_interruption() -> None:
    """不变量的整体形态：两轮工具调用，第二轮断在中途。"""
    stored = to_storage(
        [
            _ai_with_call("第一步", "t1"),
            ToolMessage(content="ok", tool_call_id="t1"),
            _ai_with_call("第二步", "t2"),
        ]
    )

    uses = [b["id"] for s in stored for b in s.content if b.get("type") == "tool_use"]
    results = [
        b["tool_use_id"] for s in stored for b in s.content if b.get("type") == "tool_result"
    ]
    assert sorted(uses) == sorted(results) == ["t1", "t2"]


# --------------------------------------------------------------------------- 入


def test_round_trip_preserves_tool_calls() -> None:
    original = [
        _ai_with_call("我来写这个文件", "toolu_1"),
        ToolMessage(content="ok", tool_call_id="toolu_1", name="write_file"),
        AIMessage(content=[{"type": "text", "text": "写好了"}]),
    ]
    restored = from_storage(to_storage(original))

    assert isinstance(restored[0], AIMessage)
    assert [c["id"] for c in restored[0].tool_calls] == ["toolu_1"]
    assert restored[0].tool_calls[0]["args"] == {"path": "a.txt"}
    assert isinstance(restored[1], ToolMessage)
    assert restored[1].tool_call_id == "toolu_1"
    assert restored[1].name == "write_file"
    assert isinstance(restored[2], AIMessage)


def test_restored_assistant_keeps_tool_use_out_of_content() -> None:
    """★ 工具调用只体现在 tool_calls 字段上。

    塞进 content 只有 Anthropic 方言认得；OpenAI 方言的 adapter 会把它
    当成一段无意义的结构体原样发出去。
    """
    restored = from_storage(
        to_storage([_ai_with_call("写", "t1"), ToolMessage(content="ok", tool_call_id="t1")])
    )

    blocks = restored[0].content
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["text"]
    assert restored[0].tool_calls  # 但调用没丢


def test_error_results_round_trip_to_error_status() -> None:
    restored = from_storage(
        [
            StoredMessage(
                role="user",
                kind=KIND_TOOL_RESULT,
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "boom",
                        "is_error": True,
                    }
                ],
            )
        ]
    )
    assert isinstance(restored[0], ToolMessage)
    assert restored[0].status == "error"


def test_legacy_rows_without_tool_blocks_still_load() -> None:
    """改造前落的库里只有对话轮 —— 那些会话必须照常读得回来。"""
    restored = from_storage(
        [
            StoredMessage(role="user", kind=KIND_CHAT, content=[{"type": "text", "text": "在吗"}]),
            StoredMessage(
                role="assistant", kind=KIND_CHAT, content=[{"type": "text", "text": "在"}]
            ),
        ]
    )
    assert isinstance(restored[0], HumanMessage)
    assert isinstance(restored[1], AIMessage)
    assert restored[1].tool_calls == []
