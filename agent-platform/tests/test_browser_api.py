"""浏览器的接口（c4-model/agent/browser/design-browser.html）：接管、我的登录、浏览器状态。

浏览器组件（宿主、守护）换成替身，登录态和审批用真的库：

    打开      确保浏览器在、开接管窗口；不是自己的 run 看不到
    交还      先存这次登录的站点、再做决定；存不下来照样交还，结果里说没存下来；不能交还两次
    不处理    做决定、尽力关窗口，不存登录态
    审批接口  通用的审批接口不能批接管（那样登录态不存）
    我的登录  列出、删除（正在跑的浏览器里也清）；状态接口看浏览器在不在跑
    剪贴板    人在画面里粘贴、复制：只给会话的创建人；浏览器没在跑回 409
    电脑上    本机 CLI 的会话：开关窗口、状态、剪贴板走设备宿主；交还不存登录态；用不了说清楚原因
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_agent.api.v1.browser import _device, get_browser
from atlas_agent.approval import ApprovalService
from atlas_agent.browser.daemon import Captured, DaemonError
from atlas_agent.browser.device import DeviceBrowserUnavailable, DeviceUnavailable
from atlas_agent.browser.login import KeyRing, LoginVault, Site, StateBundle, StateCipher
from atlas_agent.db.models import AgentVersion, Approval, Run, Thread
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.deps import get_approval_service
from atlas_agent.ids import uuid7
from atlas_agent.main import create_app
from atlas_agent.sandbox.provisioning.model import SandboxRef
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy import select, update

from tests.fake_uc import AS_USER, TEST_USER

URL = "https://login.taobao.com/"
OTHER = {"X-Uid": "00000000-0000-0000-0000-0000000000aa"}


class FakeHosts:
    def __init__(self) -> None:
        self.ref = SandboxRef(uuid7())
        self.running = True
        self.ensured: list[UUID] = []
        self.forgotten: list[tuple[UUID, Site]] = []

    async def ensure(self, thread: Any, spec: Any) -> SandboxRef:
        self.ensured.append(thread.thread_uuid)
        return self.ref

    async def current(self, thread: Any) -> SandboxRef | None:
        return self.ref if self.running else None

    async def forget(self, user: UUID, site: Site) -> None:
        self.forgotten.append((user, site))


class FakeDaemon:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.screens: list[str] = []
        self.closed: list[str] = []
        state = StateBundle.parse(
            {
                "cookies": [
                    {"name": "cookie2", "value": "v", "domain": ".taobao.com", "path": "/"},
                    {"name": "IDE", "value": "x", "domain": ".doubleclick.net", "path": "/"},
                ]
            }
        )
        self.captured = Captured(("login.taobao.com",), state)
        self.fail_close = False
        self.clipboard = ""

    async def takeover_open(
        self, ref: SandboxRef, takeover_id: str, url: str, *, screen: str
    ) -> None:
        self.opened.append((takeover_id, url))
        self.screens.append(screen)

    async def takeover_close(self, ref: SandboxRef, takeover_id: str) -> Captured:
        self.closed.append(takeover_id)
        if self.fail_close:
            raise DaemonError("守护没了")
        return self.captured

    async def clipboard_set(self, ref: SandboxRef, text: str) -> None:
        self.clipboard = text

    async def clipboard_get(self, ref: SandboxRef) -> str:
        return self.clipboard


class Listener:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def decided(self, run_id: UUID, approval_id: UUID) -> None:
        self.calls.append((run_id, approval_id))


@pytest.fixture
async def env(clean_db: None) -> Any:
    key = SecretStr(base64.b64encode(b"k" * 32).decode())
    browser = SimpleNamespace(
        hosts=FakeHosts(),
        daemon=FakeDaemon(),
        vault=LoginVault(get_sessionmaker(), StateCipher(KeyRing({1: key}))),
    )
    listener = Listener()
    app = create_app()
    app.dependency_overrides[get_browser] = lambda: browser

    async def approvals() -> Any:
        async with get_sessionmaker()() as session:
            yield ApprovalService(session, listener=listener)  # type: ignore[arg-type]

    app.dependency_overrides[get_approval_service] = approvals
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AS_USER
    ) as client:
        yield SimpleNamespace(client=client, browser=browser, listener=listener, app=app)


async def _takeover(client: httpx.AsyncClient) -> tuple[str, str, str]:
    """一个智能体、一个会话、一个挂起的 run、一件等人处理的接管。返回 (run, 接管, 会话)。"""
    spec = {"model": {"model": "claude-opus-5"}, "tool_names": ["bash"]}
    agent = await client.post("/v1/agents", json={"slug": "shopper", "name": "购物", "spec": spec})
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    assert thread.status_code == 201, thread.text
    thread_id = UUID(thread.json()["id"])
    async with get_sessionmaker()() as session:
        version = (
            await session.execute(
                select(AgentVersion).where(AgentVersion.agent_uuid == UUID(agent.json()["id"]))
            )
        ).scalar_one()
        run = Run(
            thread_uuid=thread_id,
            agent_version_uuid=version.agent_version_uuid,
            created_by_uuid=UUID(TEST_USER),
            status="suspended",
        )
        session.add(run)
        await session.flush()
        approval = Approval(
            run_uuid=run.run_uuid,
            tool_name="request_takeover",
            args={"reason": "请登录", "url": URL},
            status="pending",
            tool_call_id="c1",
            kind="takeover",
            source="browser",
            reason="请登录",
        )
        session.add(approval)
        await session.commit()
        return str(run.run_uuid), str(approval.approval_uuid), str(thread_id)


async def _approval(approval_id: str) -> Approval:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(Approval).where(Approval.approval_uuid == UUID(approval_id))
            )
        ).scalar_one()


async def test_open_ensures_the_browser_and_opens_the_window(env: Any) -> None:
    run, takeover, thread = await _takeover(env.client)
    res = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/open")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "opened" and res.json()["thread_id"] == thread
    assert env.browser.daemon.opened == [(takeover, URL)]
    # 接管窗口开在这个对话那一块屏幕上（没有根会话：对话就是它自己）
    assert env.browser.daemon.screens == [thread]
    assert env.browser.hosts.ensured == [UUID(thread)]
    # 别人的 run：看不到
    res = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/open", headers=OTHER)
    assert res.status_code == 404


async def test_hand_back_saves_the_login_before_deciding(env: Any) -> None:
    run, takeover, thread = await _takeover(env.client)
    res = await env.client.post(
        f"/v1/runs/{run}/takeovers/{takeover}/hand-back", json={"note": "登录好了"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["login"] == {"saved": True, "sites": ["taobao.com"]}
    # 只存这次登录的站点：广告的 cookie 不进库
    sites = await env.browser.vault.sites(UUID(TEST_USER))
    assert [s.site for s in sites] == ["taobao.com"]
    assert sites[0].captured_from == UUID(thread)
    approval = await _approval(takeover)
    assert approval.status == "approved"
    assert approval.outcome == {
        "feedback": "登录好了",
        "login": {"saved": True, "sites": ["taobao.com"]},
    }
    assert env.listener.calls == [(UUID(run), UUID(takeover))]
    again = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/hand-back", json={})
    assert again.status_code == 409


async def test_hand_back_still_works_when_saving_fails(env: Any) -> None:
    run, takeover, _ = await _takeover(env.client)
    env.browser.daemon.fail_close = True
    res = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/hand-back", json={})
    assert res.status_code == 200, res.text
    assert res.json()["login"] == {"saved": False, "sites": []}
    assert (await _approval(takeover)).status == "approved"


async def test_decline_decides_and_closes_the_window_without_saving(env: Any) -> None:
    run, takeover, _ = await _takeover(env.client)
    res = await env.client.post(
        f"/v1/runs/{run}/takeovers/{takeover}/decline", json={"note": "不想登录"}
    )
    assert res.status_code == 200, res.text
    approval = await _approval(takeover)
    assert approval.status == "rejected" and approval.outcome == {"feedback": "不想登录"}
    assert env.browser.daemon.closed == [takeover]
    assert await env.browser.vault.sites(UUID(TEST_USER)) == []


async def test_generic_approval_endpoint_refuses_takeovers(env: Any) -> None:
    run, takeover, _ = await _takeover(env.client)
    res = await env.client.post(
        f"/v1/runs/{run}/approvals/{takeover}", json={"decision": "approved"}
    )
    assert res.status_code == 409, res.text
    assert (await _approval(takeover)).status == "pending"


async def test_my_logins_and_browser_status(env: Any) -> None:
    run, takeover, thread = await _takeover(env.client)
    await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/hand-back", json={})
    listed = await env.client.get("/v1/browser/logins")
    assert listed.status_code == 200, listed.text
    assert [i["site"] for i in listed.json()["items"]] == ["taobao.com"]
    assert (await env.client.get("/v1/browser/logins", headers=OTHER)).json()["items"] == []

    status = await env.client.get(f"/v1/threads/{thread}/browser")
    assert status.json() == {"running": True}
    env.browser.hosts.running = False
    assert (await env.client.get(f"/v1/threads/{thread}/browser")).json() == {"running": False}

    gone = await env.client.delete("/v1/browser/logins/taobao.com")
    assert gone.status_code == 204, gone.text
    assert env.browser.hosts.forgotten == [(UUID(TEST_USER), Site("taobao.com"))]
    assert (await env.client.delete("/v1/browser/logins/taobao.com")).status_code == 404


async def test_clipboard_round_trip_only_for_the_creator_of_a_running_browser(env: Any) -> None:
    _, _, thread = await _takeover(env.client)
    url = f"/v1/threads/{thread}/browser/clipboard"
    put = await env.client.put(url, json={"text": "收货地址：杭州市西湖区"})
    assert put.status_code == 204, put.text
    assert env.browser.daemon.clipboard == "收货地址：杭州市西湖区"
    got = await env.client.get(url)
    assert got.status_code == 200, got.text
    assert got.json() == {"text": "收货地址：杭州市西湖区"}

    assert (await env.client.get(url, headers=OTHER)).status_code in (403, 404)
    env.browser.hosts.running = False
    assert (await env.client.put(url, json={"text": "x"})).status_code == 409


# ---------------------------------------------------------------------------- 电脑上的浏览器


class FakeDevice:
    """设备宿主的替身：记下请求；offline 时像电脑不在线那样拒绝。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.offline = False

    def _check(self) -> None:
        if self.offline:
            raise DeviceBrowserUnavailable(DeviceUnavailable.OFFLINE, "VPS")

    async def open_takeover(self, thread: Any, takeover_id: str, url: str) -> dict[str, Any]:
        self._check()
        self.calls.append(("open", str(thread.thread_uuid), takeover_id, url))
        return {"target": "T1"}

    async def close_takeover(self, thread: Any, takeover_id: str) -> None:
        self._check()
        self.calls.append(("close", takeover_id))

    async def running(self, thread: Any) -> bool:
        return not self.offline

    async def clipboard_set(self, thread: Any, text: str) -> None:
        self._check()
        self.calls.append(("clip", text))

    async def clipboard_get(self, thread: Any) -> str:
        self._check()
        return "电脑上的剪贴板"


async def _on_device(env: Any, thread: str) -> FakeDevice:
    """把会话绑到一台电脑上（本机 CLI 智能体的会话），设备宿主换成替身。"""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Thread).where(Thread.thread_uuid == UUID(thread)).values(device_uuid=uuid7())
        )
        await session.commit()
    device = FakeDevice()
    env.app.dependency_overrides[_device] = lambda: device
    return device


async def test_device_takeover_opens_and_hands_back_without_saving_logins(env: Any) -> None:
    run, takeover, thread = await _takeover(env.client)
    device = await _on_device(env, thread)
    res = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/open")
    assert res.status_code == 200, res.text
    res = await env.client.post(
        f"/v1/runs/{run}/takeovers/{takeover}/hand-back", json={"note": "扫过码了"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["login"] is None
    assert device.calls == [("open", thread, takeover, URL), ("close", takeover)]
    # 沙箱那边一点没碰：登录态留在电脑上，不存
    assert env.browser.daemon.opened == [] and await env.browser.vault.sites(UUID(TEST_USER)) == []
    assert (await _approval(takeover)).outcome == {"feedback": "扫过码了"}


async def test_device_offline_is_said_plainly(env: Any) -> None:
    run, takeover, thread = await _takeover(env.client)
    device = await _on_device(env, thread)
    device.offline = True
    res = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/open")
    assert res.status_code == 409
    assert "不在线" in res.json()["error"]["message"]
    status = await env.client.get(f"/v1/threads/{thread}/browser")
    assert status.json() == {"running": False}
    clip = await env.client.get(f"/v1/threads/{thread}/browser/clipboard")
    assert clip.status_code == 409
    # 不处理照样能做：电脑不在线时关不了窗口，不挡住决定
    res = await env.client.post(f"/v1/runs/{run}/takeovers/{takeover}/decline", json={})
    assert res.status_code == 200 and (await _approval(takeover)).status == "rejected"


async def test_device_clipboard_and_status(env: Any) -> None:
    _, _, thread = await _takeover(env.client)
    device = await _on_device(env, thread)
    assert (await env.client.get(f"/v1/threads/{thread}/browser")).json() == {"running": True}
    put = await env.client.put(f"/v1/threads/{thread}/browser/clipboard", json={"text": "密码"})
    assert put.status_code == 204
    got = await env.client.get(f"/v1/threads/{thread}/browser/clipboard")
    assert got.json() == {"text": "电脑上的剪贴板"}
    assert device.calls == [("clip", "密码")]
    other = await env.client.get(f"/v1/threads/{thread}/browser/clipboard", headers=OTHER)
    assert other.status_code in (403, 404)
