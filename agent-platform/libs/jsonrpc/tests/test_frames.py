"""帧的解析与编码。"""

from __future__ import annotations

import json

import pytest
from atlas_jsonrpc import (
    ErrorCode,
    InvalidFrame,
    Notification,
    Request,
    Response,
    RpcFault,
    encode,
    parse_frame,
)


def test_request_roundtrip() -> None:
    frame = Request(7, "session/prompt", {"sessionId": "s", "prompt": []})
    assert parse_frame(encode(frame)) == frame


def test_notification_roundtrip() -> None:
    frame = Notification("session/update", {"update": {"text": "你好"}})
    assert parse_frame(encode(frame)) == frame


def test_response_roundtrip() -> None:
    assert parse_frame(encode(Response(3, result={"ok": True}))) == Response(3, result={"ok": True})


def test_error_response_roundtrip() -> None:
    parsed = parse_frame(encode(Response("a", error=RpcFault(-32010, "未打开", {"cause": "x"}))))
    assert isinstance(parsed, Response) and not parsed.ok
    assert (parsed.error.code, parsed.error.message, parsed.error.data) == (
        -32010,
        "未打开",
        {"cause": "x"},
    )


def test_null_result_is_a_valid_response() -> None:
    # result 为 null 仍是成功响应，不能与「缺 result」混淆
    assert parse_frame('{"jsonrpc":"2.0","id":1,"result":null}') == Response(1, result=None)


def test_encoding_is_single_line_even_with_newlines_in_content() -> None:
    """★ ACP 的 stdio 以换行分帧，消息内部不得有真正的换行。"""
    text = encode(Notification("x", {"code": "line1\nline2\r\n"}))
    assert "\n" not in text and "\r" not in text
    assert json.loads(text)["params"]["code"] == "line1\nline2\r\n"


def test_non_ascii_is_kept_verbatim() -> None:
    assert "你好" in encode(Notification("x", {"t": "你好"}))


def test_missing_params_defaults_to_empty_object() -> None:
    assert parse_frame('{"jsonrpc":"2.0","method":"ping"}') == Notification("ping", {})


@pytest.mark.parametrize(
    ("text", "code", "reply"),
    [
        ("{not json", ErrorCode.PARSE_ERROR, True),
        ("[1,2]", ErrorCode.INVALID_REQUEST, True),
        ('"x"', ErrorCode.INVALID_REQUEST, True),
        ('{"jsonrpc":"1.0","method":"x"}', ErrorCode.INVALID_REQUEST, True),
        ('{"jsonrpc":"2.0","method":""}', ErrorCode.INVALID_REQUEST, True),
        ('{"jsonrpc":"2.0","method":"x","params":3}', ErrorCode.INVALID_REQUEST, True),
        ('{"jsonrpc":"2.0","method":"x","id":true}', ErrorCode.INVALID_REQUEST, True),
        ('{"jsonrpc":"2.0"}', ErrorCode.INVALID_REQUEST, True),
        # 形状像响应的坏帧：不回错误，避免两个端点来回互发
        ('{"jsonrpc":"2.0","id":1}', ErrorCode.INVALID_REQUEST, False),
        (
            '{"jsonrpc":"2.0","id":1,"result":1,"error":{"code":1}}',
            ErrorCode.INVALID_REQUEST,
            False,
        ),
        ('{"jsonrpc":"2.0","id":1,"error":"boom"}', ErrorCode.INVALID_REQUEST, False),
    ],
)
def test_invalid_frames(text: str, code: int, reply: bool) -> None:
    with pytest.raises(InvalidFrame) as info:
        parse_frame(text)
    assert info.value.fault.code == code
    assert info.value.reply is reply


def test_invalid_request_keeps_its_id_for_the_error_reply() -> None:
    with pytest.raises(InvalidFrame) as info:
        parse_frame('{"jsonrpc":"2.0","id":9,"method":"x","params":"bad"}')
    assert info.value.request_id == 9
