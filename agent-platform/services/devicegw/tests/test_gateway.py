"""设备网关：配对、控制连接与在线表、会话转发、文件转发、能力代理、吊销。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from atlas_devicelink import Channel
from gw_kit import DEVICE, INTERNAL_AUTH, OWNER, Gateway, fake_connector, server_side
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

THREAD = "0193f1a0-0000-7000-8000-000000000010"


async def register(gw: Gateway, thread: str = THREAD) -> dict:
    async with httpx.AsyncClient(base_url=gw.url, trust_env=False, headers=INTERNAL_AUTH) as client:
        resp = await client.put(
            f"/internal/sessions/{thread}",
            json={
                "device": DEVICE,
                "owner": OWNER,
                "launch": {
                    "cli_type": "claude-code",
                    "project_dir": "~/code/shop-web",
                    "mode": "default",
                    "model": {"upstream": "m1", "model": "MiniMax-M3.1-Flash"},
                },
                "upstreams": [
                    {
                        "id": "m1",
                        "kind": "model",
                        "url": "http://litellm:4000",
                        "headers": {"Authorization": "Bearer sk-agent"},
                    },
                    {
                        "id": "s1",
                        "kind": "mcp",
                        "url": "http://mcpgw:8070/mcp/serpapi",
                        "headers": {"Authorization": "Bearer mk-agent", "X-Uid": OWNER},
                    },
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()


async def never(_: Channel) -> None:
    await asyncio.sleep(3600)


# ------------------------------------------------------------------ 配对、控制连接


async def test_pair_redeems_code_and_rejects_bad_ones(gateway: Gateway) -> None:
    async with httpx.AsyncClient(
        base_url=gateway.url, trust_env=False, headers=INTERNAL_AUTH
    ) as client:
        ok = await client.post("/pair", json={"code": "k7m2-q9xd", "report": {}})
        bad = await client.post("/pair", json={"code": "XXXX-YYYY", "report": {}})
    assert ok.status_code == 200 and ok.json()["token"] == "dev_good"
    assert bad.status_code == 400 and bad.json()["error"]["kind"] == "invalid_code"


async def test_connect_writes_presence_and_disconnect_drops_it(gateway: Gateway) -> None:
    async with httpx.AsyncClient(
        base_url=gateway.url, trust_env=False, headers=INTERNAL_AUTH
    ) as client:
        async with fake_connector(gateway, never) as (_, _, welcome):
            assert welcome == {"t": "welcome", "device": DEVICE, "name": "MacBook Pro"}
            found = (await client.get(f"/internal/presence/{DEVICE}")).json()
            assert found["instance"] == "http://gw-1:8095"
            assert found["report"]["clis"] == {"claude-code": "2.1"}
            assert found["report"]["connector_version"] == "1.0.0"
        await asyncio.sleep(0.2)
        assert (await client.get(f"/internal/presence/{DEVICE}")).status_code == 404


@pytest.mark.parametrize(
    ("token", "version", "code"),
    [("dev_good", "0.9.0", 4426), ("dev_bad", "1.0.0", 4401)],
)
async def test_old_connector_or_bad_token_is_refused(
    gateway: Gateway, token, version, code
) -> None:
    with pytest.raises(ConnectionClosed) as info:
        async with fake_connector(gateway, never, token=token, version=version):
            pass
    assert info.value.rcvd.code == code


async def test_new_connection_supersedes_the_old_one(gateway: Gateway) -> None:
    async with fake_connector(gateway, never) as (old_ws, _, _), fake_connector(gateway, never):
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(old_ws.recv(), 2)
        assert old_ws.close_code == 4409


# ------------------------------------------------------------------ 会话转发


async def test_session_relay_round_trip_and_close_reason(gateway: Gateway) -> None:
    grant = await register(gateway)
    opened: asyncio.Queue[Channel] = asyncio.Queue()

    async def bridge(channel: Channel) -> None:
        await opened.put(channel)
        async for message in channel:  # 假 bridge：把请求原样回过去
            await channel.send(message)

    async with fake_connector(gateway, bridge):
        ws = await server_side(gateway, THREAD)
        assert ws.subprotocol == "atlas.host.v1"
        channel = await asyncio.wait_for(opened.get(), 2)
        head = channel.head
        assert head["thread"] == THREAD and head["project_dir"] == "~/code/shop-web"
        assert head["model_env"]["base_url"] == "https://atlas.example/device-api/u/m1"
        assert head["model_env"]["token"] == grant["token"]
        assert "upstreams" not in head  # 真实凭据不出网关
        await ws.send('{"jsonrpc":"2.0","id":1,"method":"session.open"}')
        assert (
            await asyncio.wait_for(ws.recv(), 2)
            == '{"jsonrpc":"2.0","id":1,"method":"session.open"}'
        )
        await channel.close(4410, "session ended")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 2)
        assert ws.close_code == 4410


async def test_device_not_on_this_instance_tells_server_to_reconnect(gateway: Gateway) -> None:
    await register(gateway)
    ws = await server_side(gateway, THREAD)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 2)
    assert ws.close_code == 1001


async def test_losing_the_device_mid_session_tells_server_to_reconnect(gateway: Gateway) -> None:
    await register(gateway)
    opened: asyncio.Queue[Channel] = asyncio.Queue()

    async def bridge(channel: Channel) -> None:
        await opened.put(channel)
        await asyncio.sleep(3600)

    async with fake_connector(gateway, bridge) as (device_ws, _, _):
        ws = await server_side(gateway, THREAD)
        await asyncio.wait_for(opened.get(), 2)
        await device_ws.close()
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 2)
        assert ws.close_code == 1001


async def test_host_relay_checks_internal_token_and_session_header(gateway: Gateway) -> None:
    await register(gateway)
    with pytest.raises(InvalidStatus):
        await server_side(gateway, THREAD, token="wrong")


# ------------------------------------------------------------------ 能力代理


async def test_proxy_swaps_credentials_and_streams_back(gateway: Gateway) -> None:
    grant = await register(gateway)
    # 本机 CLI 这一侧：不带服务令牌（_bearer 先看 Authorization，带了就把会话令牌顶掉了）
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
        resp = await client.post(
            "/u/m1/v1/messages?beta=true",
            headers={
                "Authorization": f"Bearer {grant['token']}",
                "X-Uid": "someone-else",
                "anthropic-version": "2023-06-01",
            },
            content=b'{"model":"x"}',
        )
        assert resp.status_code == 200 and resp.text == "data: a\n\ndata: b\n\n"
        sent = gateway.upstream.seen[-1]
        assert str(sent.url) == "http://litellm:4000/v1/messages?beta=true"
        assert sent.headers["authorization"] == "Bearer sk-agent"
        assert "x-uid" not in sent.headers  # 设备带来的身份去掉
        assert sent.headers["anthropic-version"] == "2023-06-01"
        assert sent.content == b'{"model":"x"}'

        mcp = await client.post("/u/s1", headers={"x-api-key": grant["token"]}, content=b"{}")
        assert mcp.status_code == 200
        sent = gateway.upstream.seen[-1]
        assert str(sent.url) == "http://mcpgw:8070/mcp/serpapi"
        assert sent.headers["x-uid"] == OWNER

        assert (
            await client.post("/u/m1/v1/messages", headers={"Authorization": "Bearer nope"})
        ).status_code == 401
        assert (
            await client.get("/u/zz", headers={"Authorization": f"Bearer {grant['token']}"})
        ).status_code == 404


async def test_internal_http_endpoints_need_the_service_token(gateway: Gateway) -> None:
    """★ 内部 HTTP 接口和 WS 一样校验服务令牌：nginx 挡掉 /internal 只是第一道，集群里谁连得上
    设备网关都能调（登记会话能力、读使用者电脑上的文件）。"""
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
        for method, path, body in (
            ("GET", f"/internal/presence/{DEVICE}", None),
            (
                "PUT",
                f"/internal/sessions/{THREAD}",
                {"device": DEVICE, "owner": OWNER, "launch": {}},
            ),
            ("DELETE", f"/internal/sessions/{THREAD}", None),
            ("POST", f"/internal/files/{DEVICE}", {"op": "list"}),
            ("POST", f"/internal/browser/{DEVICE}", {"op": "status"}),
            ("POST", f"/internal/devices/{DEVICE}/disconnect", {}),
        ):
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                resp = await client.request(method, path, json=body, headers=headers)
                assert resp.status_code == 401, (method, path, headers)


async def test_proxy_refuses_dot_segments(gateway: Gateway) -> None:
    """★ httpx 会把点段规整掉：上游是 …/mcp/serpapi 时，../../internal 就出了这个上游的范围，
    带着换上的真实凭据和身份去调同一台主机上的别的接口。

    客户端发 %2e%2e（httpx 不规整编码过的点）。..%3f、..%23 解码成 ..? ..#，httpx 把它们之后
    当成查询串和片段、前面的 .. 照样规整；%252f 解码一次是 %2f，发出去被当成 /。"""
    grant = await register(gateway)
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
        for path in (
            "/u/s1/%2e%2e/%2e%2e/internal/v1/x",
            "/u/s1/..%3fx",
            "/u/s1/..%23",
            "/u/s1/..%252f..%252finternal",
        ):
            resp = await client.post(
                path, headers={"Authorization": f"Bearer {grant['token']}"}, content=b"{}"
            )
            assert resp.status_code == 400, path
    assert not gateway.upstream.seen


async def test_token_stays_the_same_across_turns(gateway: Gateway) -> None:
    first = await register(gateway)
    second = await register(gateway)
    assert first["token"] == second["token"]


# ------------------------------------------------------------------ 文件转发


async def test_file_relay_streams_fetch_and_answers_list(gateway: Gateway) -> None:
    async def files(channel: Channel) -> None:
        head = channel.head
        if head["op"] == "fetch":
            await channel.send(
                json.dumps({"status": 200, "headers": {"ETag": '"v1"', "Content-Length": "6"}})
            )
            await channel.send(b"abc")
            await channel.send(b"def")
        else:
            await channel.send(json.dumps({"status": 200, "body": {"entries": [{"name": "src"}]}}))
        await channel.close()

    async with httpx.AsyncClient(
        base_url=gateway.url, trust_env=False, headers=INTERNAL_AUTH
    ) as client:
        offline = await client.post(
            f"/internal/files/{DEVICE}", json={"op": "list", "project_dir": "~/x"}
        )
        assert offline.status_code == 503 and offline.json()["error"]["kind"] == "device_offline"
        async with fake_connector(gateway, files):
            got = await client.post(
                f"/internal/files/{DEVICE}",
                json={"op": "fetch", "project_dir": "~/x", "path": "a.txt"},
            )
            assert (
                got.status_code == 200
                and got.content == b"abcdef"
                and got.headers["etag"] == '"v1"'
            )
            listed = await client.post(
                f"/internal/files/{DEVICE}", json={"op": "list", "project_dir": "~/x"}
            )
            assert listed.json() == {"entries": [{"name": "src"}]}


# ------------------------------------------------------------------ 本机浏览器


async def test_browser_relay_asks_the_connector_and_passes_rejections(gateway: Gateway) -> None:
    seen: list[dict] = []

    async def browser(channel: Channel) -> None:
        seen.append(channel.head)
        if channel.head["op"] == "takeover.open":
            body = {"error": {"kind": "remote_view_off", "message": "电脑主人关闭了远程查看画面"}}
            await channel.send(json.dumps({"status": 403, "body": body}))
        else:
            await channel.send(json.dumps({"status": 200, "body": {"running": True, "slot": 1}}))
        await channel.close()

    async with httpx.AsyncClient(
        base_url=gateway.url, trust_env=False, headers=INTERNAL_AUTH
    ) as client:
        offline = await client.post(f"/internal/browser/{DEVICE}", json={"op": "status"})
        assert offline.status_code == 503 and offline.json()["error"]["kind"] == "device_offline"
        async with fake_connector(gateway, browser):
            status = await client.post(
                f"/internal/browser/{DEVICE}", json={"op": "status", "screen": "root-1"}
            )
            assert status.status_code == 200 and status.json() == {"running": True, "slot": 1}
            refused = await client.post(
                f"/internal/browser/{DEVICE}",
                json={
                    "op": "takeover.open",
                    "takeover": "a1",
                    "screen": "root-1",
                    "url": "https://x",
                },
            )
            assert refused.status_code == 403
            assert refused.json()["error"]["message"] == "电脑主人关闭了远程查看画面"
            bad = await client.post(f"/internal/browser/{DEVICE}", json={"op": "exec"})
            assert bad.status_code == 422
    assert seen[0] == {"op": "status", "thread": "", "screen": "root-1", "takeover": "", "url": ""}


async def test_view_relay_pipes_bytes_and_passes_the_connector_close_code(
    gateway: Gateway,
) -> None:
    async def view(channel: Channel) -> None:
        if channel.head["screen"] == "never-used":
            await channel.close(4404, "这个对话还没用过浏览器")
            return
        await channel.send(b"RFB 003.008\n")
        echo = await channel.receive()
        await channel.send(b"echo:" + echo)  # type: ignore[operator]
        await channel.close(4502, "画面服务断了")

    url = f"{gateway.ws}/internal/view/{DEVICE}"
    auth = {"Authorization": "Bearer internal-secret"}
    async with fake_connector(gateway, view):
        async with connect(
            f"{url}?thread=t1&screen=root-1", additional_headers=auth, proxy=None
        ) as ws:
            assert await ws.recv() == b"RFB 003.008\n"
            await ws.send(b"RFB 003.008\n")
            assert await ws.recv() == b"echo:RFB 003.008\n"
            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert ws.close_code == 4502
        async with connect(
            f"{url}?thread=t1&screen=never-used", additional_headers=auth, proxy=None
        ) as ws:
            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert (ws.close_code, ws.close_reason) == (4404, "这个对话还没用过浏览器")
        with pytest.raises(InvalidStatus):
            async with connect(f"{url}?screen=x", proxy=None):
                pass


# ------------------------------------------------------------------ 吊销


async def test_revoking_a_device_drops_sessions_and_closes_the_link(gateway: Gateway) -> None:
    grant = await register(gateway)
    async with httpx.AsyncClient(
        base_url=gateway.url, trust_env=False, headers=INTERNAL_AUTH
    ) as client:
        async with fake_connector(gateway, never) as (device_ws, mux, _):
            result = (
                await client.post(
                    f"/internal/devices/{DEVICE}/disconnect", json={"reason": "已吊销"}
                )
            ).json()
            assert result == {"disconnected": True, "sessions": [THREAD]}
            for _ in range(100):
                if mux.done:
                    break
                await asyncio.sleep(0.02)
            assert device_ws.close_code == 4401
        denied = await client.post(
            "/u/m1/v1/messages", headers={"Authorization": f"Bearer {grant['token']}"}
        )
        assert denied.status_code == 401
