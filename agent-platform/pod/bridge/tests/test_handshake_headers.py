"""握手头的会话绑定（Bridge 设计 §4.3）：X-Session-Id 必须等于本 Pod 的会话。

请求头名称不带应用名（数字员工设计 §7.4）：旧名 X-Atlas-Session 过渡一个版本仍然认，
升级期间新 server 连旧 Pod、旧 server 连新 Pod 都能握手。
"""

from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from typing import Any

from atlas_bridge.upstream.server import UpstreamServer
from atlas_host import HEADER_SESSION, LEGACY_HEADER_SESSION, SUBPROTOCOL_V1
from websockets.datastructures import Headers


class _Conn:
    remote_address = ("127.0.0.1", 1)

    def respond(self, status: HTTPStatus, text: str) -> tuple[int, str]:
        return int(status), text


def _check(**headers: str) -> Any:
    server = UpstreamServer(
        SimpleNamespace(phase=None),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        token="t",
        session_id="s-1",
        listen_host="127.0.0.1",
        listen_port=0,
    )
    h = Headers({"Authorization": "Bearer t", "Sec-WebSocket-Protocol": SUBPROTOCOL_V1})
    for k, v in headers.items():
        h[k] = v
    return server._process_request(_Conn(), SimpleNamespace(path="/host", headers=h))  # type: ignore[arg-type]


def test_session_header_names() -> None:
    assert HEADER_SESSION == "X-Session-Id"
    assert _check(**{HEADER_SESSION: "s-1"}) is None
    assert _check(**{LEGACY_HEADER_SESSION: "s-1"}) is None
    # 新旧都发（新 server）：以新名为准
    assert _check(**{HEADER_SESSION: "s-1", LEGACY_HEADER_SESSION: "s-1"}) is None
    assert _check(**{HEADER_SESSION: "other"})[0] == 403
    assert _check()[0] == 403
