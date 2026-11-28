"""浏览器的接口（c4-model/agent/browser/design-browser.html）。

    POST   /v1/runs/{run}/takeovers/{id}/open        打开接管：确保浏览器在，开接管窗口
    POST   /v1/runs/{run}/takeovers/{id}/hand-back   交还：先存这次登录的站点，再唤醒
    POST   /v1/runs/{run}/takeovers/{id}/decline     不处理
    GET    /v1/threads/{thread}/browser              这个会话的浏览器在不在跑（有没有画面）
    PUT    /v1/threads/{thread}/browser/clipboard    人在画面里粘贴：文字先放进浏览器的剪贴板
    GET    /v1/threads/{thread}/browser/clipboard    人在画面里复制：取浏览器剪贴板里的文字
    WS     /v1/threads/{thread}/browser/view         实时画面（noVNC 连这里；同源，
                                                     网关照常校验登录）
    GET    /v1/browser/logins                        我的登录：存了哪些站点
    DELETE /v1/browser/logins/{site}                 删一个站点（正在跑的浏览器里也尽力清掉）

★ 接管和画面只给会话的创建人：浏览器里是他登录后的网页，接管时还在输他的密码。
★ 按会话的浏览器在哪选路：沙箱（原生、CLI 智能体）或使用者自己的电脑（本机 CLI 智能体，经设备网关，
  browser/device.py）。网页这一头不变。
★ 画面是 WebSocket：挂在不带请求依赖的路由上，身份按同一套请求头、用短会话解析（长连接不占着
  请求作用域的数据库会话）。
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, WebSocket

from ...auth.authz import PrincipalDep, ensure_run_owner, ensure_thread_owner
from ...auth.dependency import principal_of
from ...auth.principal import Principal
from ...browser.component import BrowserComponent
from ...browser.daemon import DaemonError
from ...browser.device import (
    DeviceBrowserHost,
    DeviceBrowserUnavailable,
    DeviceUnavailable,
    on_device,
)
from ...browser.host import BrowserUnavailable
from ...browser.login import Site
from ...browser.takeover import TakeoverDesk
from ...browser.view import CloseReason, DeviceView, LiveView, ViewAccess, ViewDenied, close
from ...config import get_settings
from ...db.models import Thread
from ...db.session import fetch_one, get_sessionmaker
from ...deps import ApprovalServiceDep, RedisDep, SessionDep, SettingsDep
from ...device.gateway import DeviceGateway, GatewayUnavailable
from ...errors import AppError, Conflict, Forbidden, NotFound
from ...redisx import make_redis
from ...sandbox.component import SandboxComponent
from ...sandbox.provisioning.model import SandboxRef
from ...schemas.browser import (
    BrowserClipboard,
    BrowserLoginList,
    BrowserLoginOut,
    BrowserStatusOut,
    TakeoverNote,
    TakeoverOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["browser"])
#: 画面（WebSocket）：不带 router 级的请求依赖（它们按 HTTP 请求取身份）
ws_router = APIRouter(tags=["browser"])


class BrowserDisabled(Conflict):
    kind = "browser_disabled"


async def get_browser(settings: SettingsDep, redis: RedisDep) -> BrowserComponent | None:
    """沙箱里的浏览器；这个部署没配浏览器能力时为 None（电脑上的浏览器不需要它）。"""
    if not settings.sandbox_enabled:
        return None
    return BrowserComponent.attach(settings, SandboxComponent.build(settings, redis))


BrowserDep = Annotated[BrowserComponent | None, Depends(get_browser)]


def _sandbox(browser: BrowserComponent | None) -> BrowserComponent:
    if browser is None:
        raise BrowserDisabled("这个部署没有开浏览器能力")
    return browser


def _device(session: SessionDep, settings: SettingsDep) -> DeviceBrowserHost | None:
    """电脑上的浏览器；没部署设备网关时为 None。"""
    gateway = DeviceGateway(settings)
    return DeviceBrowserHost(session, gateway) if gateway.configured else None


DeviceDep = Annotated[DeviceBrowserHost | None, Depends(_device)]


def _desk(
    session: SessionDep, approvals: ApprovalServiceDep, browser: BrowserDep, device: DeviceDep
) -> TakeoverDesk:
    if browser is None:
        return TakeoverDesk(session, approvals, None, None, None, device=device)
    return TakeoverDesk(
        session, approvals, browser.hosts, browser.daemon, browser.vault, device=device
    )


DeskDep = Annotated[TakeoverDesk, Depends(_desk)]


# ---------------------------------------------------------------------------- 接管


@router.post("/runs/{run_id}/takeovers/{approval_id}/open", response_model=TakeoverOut)
async def open_takeover(
    run_id: UUID, approval_id: UUID, session: SessionDep, principal: PrincipalDep, desk: DeskDep
) -> TakeoverOut:
    """可以重复点：同一个接管只开一个窗口。浏览器被闲置回收了，这里重建并导入登录态（要几十秒）。"""
    await ensure_run_owner(session, run_id, principal)  # 看得到就能接管（前端直连）
    _same_run((await desk.get(approval_id)).run.run_uuid, run_id)
    takeover = await desk.open(principal.uid, approval_id)
    return TakeoverOut(
        approval_id=approval_id,
        run_id=run_id,
        thread_id=takeover.thread.thread_uuid,
        status="opened",
    )


@router.post("/runs/{run_id}/takeovers/{approval_id}/hand-back", response_model=TakeoverOut)
async def hand_back_takeover(
    run_id: UUID,
    approval_id: UUID,
    payload: TakeoverNote,
    session: SessionDep,
    principal: PrincipalDep,
    desk: DeskDep,
) -> TakeoverOut:
    await ensure_run_owner(session, run_id, principal)  # 看得到就能接管（前端直连）
    takeover = await desk.get(approval_id)
    _same_run(takeover.run.run_uuid, run_id)
    result = await desk.hand_back(principal.uid, approval_id, payload.note)
    return TakeoverOut(
        approval_id=approval_id,
        run_id=run_id,
        thread_id=takeover.thread.thread_uuid,
        status="approved",
        login=result["login"],
    )


@router.post("/runs/{run_id}/takeovers/{approval_id}/decline", response_model=TakeoverOut)
async def decline_takeover(
    run_id: UUID,
    approval_id: UUID,
    payload: TakeoverNote,
    session: SessionDep,
    principal: PrincipalDep,
    desk: DeskDep,
) -> TakeoverOut:
    await ensure_run_owner(session, run_id, principal)  # 看得到就能接管（前端直连）
    takeover = await desk.get(approval_id)
    _same_run(takeover.run.run_uuid, run_id)
    await desk.decline(principal.uid, approval_id, payload.note)
    return TakeoverOut(
        approval_id=approval_id,
        run_id=run_id,
        thread_id=takeover.thread.thread_uuid,
        status="rejected",
    )


def _same_run(actual: UUID, run_id: UUID) -> None:
    if actual != run_id:
        raise NotFound("接管不存在", run_id=str(run_id))


# ---------------------------------------------------------------------------- 浏览器状态


@router.get("/threads/{thread_id}/browser", response_model=BrowserStatusOut)
async def browser_status(
    thread_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    browser: BrowserDep,
    device: DeviceDep,
) -> BrowserStatusOut:
    await ensure_thread_owner(session, thread_id, principal)
    thread = await fetch_one(session, Thread, Thread.thread_uuid == thread_id)
    if thread is None or not _operates(thread, principal):
        return BrowserStatusOut(running=False)
    if on_device(thread):
        # 电脑上：浏览器在跑、这个对话用过它（有自己那一块）才有画面
        return BrowserStatusOut(running=device is not None and await device.running(thread))
    if browser is None:
        return BrowserStatusOut(running=False)
    return BrowserStatusOut(running=await browser.hosts.current(thread) is not None)


class BrowserNotRunning(Conflict):
    kind = "browser_not_running"


def _operates(thread: Thread, principal: Principal) -> bool:
    """能不能看、操作这个会话的浏览器（调用方已判过看得到这个会话）。以人为权限只有创建人：浏览器里
    是他登录后的网页；以数字员工为权限，有使用权的人都行：浏览器和登录态都归数字员工。"""
    return thread.owned_by_agent or thread.created_by_uuid == principal.uid


async def _owned(session: SessionDep, thread_id: UUID, principal: PrincipalDep) -> Thread:
    """能操作这个浏览器的人（同画面）。"""
    await ensure_thread_owner(session, thread_id, principal, write=True)
    thread = await fetch_one(session, Thread, Thread.thread_uuid == thread_id)
    if thread is None or not _operates(thread, principal):
        raise Forbidden("只有会话的创建人能操作这个浏览器")
    return thread


async def _running(thread: Thread, browser: BrowserComponent | None) -> SandboxRef:
    """正在跑的沙箱浏览器。不为此新建沙箱。"""
    ref = await _sandbox(browser).hosts.current(thread)
    if ref is None:
        raise BrowserNotRunning("浏览器没在运行")
    return ref


def _on_device(device: DeviceBrowserHost | None) -> DeviceBrowserHost:
    if device is None:
        raise BrowserNotRunning("这个部署没有设备网关，用不了电脑上的浏览器")
    return device


@router.put("/threads/{thread_id}/browser/clipboard", status_code=204)
async def set_clipboard(
    thread_id: UUID,
    payload: BrowserClipboard,
    session: SessionDep,
    principal: PrincipalDep,
    browser: BrowserDep,
    device: DeviceDep,
) -> Response:
    """人在画面里粘贴：画面先调这里，再给浏览器按 Ctrl+V。"""
    thread = await _owned(session, thread_id, principal)
    try:
        if on_device(thread):
            await _on_device(device).clipboard_set(thread, payload.text)
        else:
            await _sandbox(browser).daemon.clipboard_set(
                await _running(thread, browser), payload.text
            )
    except (DaemonError, BrowserUnavailable, GatewayUnavailable) as exc:
        raise BrowserNotRunning(f"粘贴失败：{exc}") from exc
    return Response(status_code=204)


@router.get("/threads/{thread_id}/browser/clipboard", response_model=BrowserClipboard)
async def get_clipboard(
    thread_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
    browser: BrowserDep,
    device: DeviceDep,
) -> BrowserClipboard:
    """人在画面里复制：画面先给浏览器按 Ctrl+C，再取这里。"""
    thread = await _owned(session, thread_id, principal)
    try:
        if on_device(thread):
            return BrowserClipboard(text=await _on_device(device).clipboard_get(thread))
        ref = await _running(thread, browser)
        return BrowserClipboard(text=await _sandbox(browser).daemon.clipboard_get(ref))
    except (DaemonError, BrowserUnavailable, GatewayUnavailable) as exc:
        raise BrowserNotRunning(f"复制失败：{exc}") from exc


# ---------------------------------------------------------------------------- 我的登录


@router.get("/browser/logins", response_model=BrowserLoginList)
async def list_logins(principal: PrincipalDep, browser: BrowserDep) -> BrowserLoginList:
    """平台保管的登录态（沙箱浏览器）。电脑上的浏览器的登录留在那台电脑上，不在这里。"""
    items = await _sandbox(browser).vault.sites(principal.uid)
    return BrowserLoginList(
        items=[
            BrowserLoginOut(
                site=s.site,
                captured_at=s.captured_at,
                captured_from=s.captured_from,
                expires_at=s.expires_at,
                expired=s.expired,
            )
            for s in items
        ]
    )


@router.delete("/browser/logins/{site}", status_code=204)
async def forget_login(site: str, principal: PrincipalDep, browser: BrowserDep) -> Response:
    """只影响以后：库里删掉，正在跑的浏览器里也尽力清掉。"""
    try:
        target = Site.of(site)
    except ValueError as exc:
        raise NotFound("没有这个站点的登录") from exc
    browser = _sandbox(browser)
    if not await browser.vault.forget(principal.uid, target):
        raise NotFound("没有这个站点的登录", site=site)
    await browser.hosts.forget(principal.uid, target)
    return Response(status_code=204)


# ---------------------------------------------------------------------------- 画面


@ws_router.websocket("/threads/{thread_id}/browser/view")
async def browser_view(websocket: WebSocket, thread_id: UUID) -> None:
    """只给会话的创建人；浏览器没在运行就带着原因关掉，不为看画面建沙箱；连着的时候算沙箱的活动。

    电脑上的浏览器（本机 CLI 智能体）：画面经设备网关的画面流，连接器接到那一块的画面服务。
    """
    settings = get_settings()
    subprotocols = list(websocket.scope.get("subprotocols") or [])
    try:
        async with get_sessionmaker()() as session:
            principal = await principal_of(websocket, session, settings)
            await ensure_thread_owner(session, thread_id, principal)  # 看不到的按 NOT_OWNER 关
            thread = await fetch_one(session, Thread, Thread.thread_uuid == thread_id)
            if thread is None or not _operates(thread, principal):
                await close(websocket, CloseReason.NOT_OWNER)
                return
            route = None
            if on_device(thread):
                gateway = DeviceGateway(settings)
                if not gateway.configured:
                    await close(websocket, CloseReason.NO_BROWSER)
                    return
                route = await DeviceBrowserHost(session, gateway).view_route(thread)
    except DeviceBrowserUnavailable as exc:
        await close(websocket, _DEVICE_CLOSE.get(exc.reason, CloseReason.NO_BROWSER))
        return
    except (BrowserUnavailable, GatewayUnavailable):
        await close(websocket, CloseReason.DEVICE_OFFLINE)
        return
    except AppError:
        await close(websocket, CloseReason.NOT_OWNER)
        return
    if route is not None:
        await DeviceView().connect(websocket, route, subprotocols=subprotocols)
        return
    await _sandbox_view(websocket, thread_id, subprotocols)


#: 电脑上的浏览器用不了的原因 → 画面的关闭原因（关闭原因最多 123 字节：话要短）
_DEVICE_CLOSE = {
    DeviceUnavailable.OFFLINE: CloseReason.DEVICE_OFFLINE,
    DeviceUnavailable.NOT_INSTALLED: CloseReason.NO_BROWSER,
    DeviceUnavailable.REMOTE_VIEW_OFF: CloseReason.DEVICE_REFUSED,
    DeviceUnavailable.REVOKED: CloseReason.NOT_OWNER,
    DeviceUnavailable.NOT_OWNER: CloseReason.NOT_OWNER,
}


async def _sandbox_view(websocket: WebSocket, thread_id: UUID, subprotocols: list[str]) -> None:
    settings = get_settings()
    if not settings.sandbox_enabled:
        await close(websocket, CloseReason.NO_BROWSER)
        return
    redis = make_redis(settings)
    try:
        sandbox = SandboxComponent.build(settings, redis)
        browser = BrowserComponent.attach(settings, sandbox)
        if browser is None:
            await close(websocket, CloseReason.NO_BROWSER)
            return
        live = LiveView(
            ViewAccess(browser.hosts, browser.keys, browser.view_port, browser.daemon),
            sandbox.channels,
        )
        try:
            async with get_sessionmaker()() as session:
                principal = await principal_of(websocket, session, settings)
                target = await live.check(session, principal.uid, thread_id)
        except ViewDenied as exc:
            await close(websocket, exc.reason)
            return
        except AppError:
            await close(websocket, CloseReason.NOT_OWNER)
            return
        await live.connect(websocket, target, subprotocols=subprotocols)
    finally:
        await redis.aclose()
