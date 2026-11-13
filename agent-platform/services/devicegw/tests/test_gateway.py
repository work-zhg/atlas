"""设备网关：配对、控制连接与在线表、会话转发、文件转发、能力代理、吊销。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from atlas_devicelink import Channel
from gw_kit import DEVICE, OWNER, Gateway, fake_connector, server_side
from websockets.exceptions import ConnectionClosed, InvalidStatus

THREAD = "0193f1a0-0000-7000-8000-000000000010"


async def register(gw: Gateway, thread: str = THREAD) -> dict:
    async with httpx.AsyncClient(base_url=gw.url, trust_env=False) as client:
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
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
        ok = await client.post("/pair", json={"code": "k7m2-q9xd", "report": {}})
        bad = await client.post("/pair", json={"code": "XXXX-YYYY", "report": {}})
    assert ok.status_code == 200 and ok.json()["token"] == "dev_good"
    assert bad.status_code == 400 and bad.json()["error"]["kind"] == "invalid_code"


async def test_connect_writes_presence_and_disconnect_drops_it(gateway: Gateway) -> None:
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
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

    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
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


# ------------------------------------------------------------------ 吊销


async def test_revoking_a_device_drops_sessions_and_closes_the_link(gateway: Gateway) -> None:
    grant = await register(gateway)
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as client:
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
