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
from ...browser.component import BrowserComponent
from ...browser.daemon import DaemonError
from ...browser.login import Site
from ...browser.takeover import TakeoverDesk
from ...browser.view import CloseReason, LiveView, ViewAccess, ViewDenied, close
from ...config import get_settings
from ...db.models import Thread
from ...db.session import fetch_one, get_sessionmaker
from ...deps import ApprovalServiceDep, RedisDep, SessionDep, SettingsDep
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


async def get_browser(settings: SettingsDep, redis: RedisDep) -> BrowserComponent:
    sandbox = SandboxComponent.build(settings, redis)
    browser = BrowserComponent.attach(settings, sandbox) if settings.sandbox_enabled else None
    if browser is None:
        raise BrowserDisabled("这个部署没有开浏览器能力")
    return browser


BrowserDep = Annotated[BrowserComponent, Depends(get_browser)]


def _desk(session: SessionDep, approvals: ApprovalServiceDep, browser: BrowserDep) -> TakeoverDesk:
    return TakeoverDesk(session, approvals, browser.hosts, browser.daemon, browser.vault)


DeskDep = Annotated[TakeoverDesk, Depends(_desk)]


# ---------------------------------------------------------------------------- 接管


@router.post("/runs/{run_id}/takeovers/{approval_id}/open", response_model=TakeoverOut)
async def open_takeover(
    run_id: UUID, approval_id: UUID, session: SessionDep, principal: PrincipalDep, desk: DeskDep
) -> TakeoverOut:
    """可以重复点：同一个接管只开一个窗口。浏览器被闲置回收了，这里重建并导入登录态（要几十秒）。"""
    await ensure_run_owner(session, run_id, principal, write=True)
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
    await ensure_run_owner(session, run_id, principal, write=True)
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
    await ensure_run_owner(session, run_id, principal, write=True)
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
    thread_id: UUID, session: SessionDep, principal: PrincipalDep, browser: BrowserDep
) -> BrowserStatusOut:
    await ensure_thread_owner(session, thread_id, principal)
    thread = await fetch_one(session, Thread, Thread.thread_uuid == thread_id)
    if thread is None or thread.created_by_uuid != principal.uid:
        return BrowserStatusOut(running=False)
    return BrowserStatusOut(running=await browser.hosts.current(thread) is not None)


class BrowserNotRunning(Conflict):
    kind = "browser_not_running"


async def _running(
    session: SessionDep, thread_id: UUID, principal: PrincipalDep, browser: BrowserComponent
) -> SandboxRef:
    """会话创建人的、正在跑的浏览器（同画面：浏览器里是他登录后的网页）。不为此新建沙箱。"""
    await ensure_thread_owner(session, thread_id, principal, write=True)
    thread = await fetch_one(session, Thread, Thread.thread_uuid == thread_id)
    if thread is None or thread.created_by_uuid != principal.uid:
        raise Forbidden("只有会话的创建人能操作这个浏览器")
    ref = await browser.hosts.current(thread)
    if ref is None:
        raise BrowserNotRunning("浏览器没在运行")
    return ref


@router.put("/threads/{thread_id}/browser/clipboard", status_code=204)
async def set_clipboard(
    thread_id: UUID,
    payload: BrowserClipboard,
    session: SessionDep,
    principal: PrincipalDep,
    browser: BrowserDep,
) -> Response:
    """人在画面里粘贴：画面先调这里，再给浏览器按 Ctrl+V。"""
    ref = await _running(session, thread_id, principal, browser)
    try:
        await browser.daemon.clipboard_set(ref, payload.text)
    except DaemonError as exc:
        raise BrowserNotRunning(f"粘贴失败：{exc}") from exc
    return Response(status_code=204)


@router.get("/threads/{thread_id}/browser/clipboard", response_model=BrowserClipboard)
async def get_clipboard(
    thread_id: UUID, session: SessionDep, principal: PrincipalDep, browser: BrowserDep
) -> BrowserClipboard:
    """人在画面里复制：画面先给浏览器按 Ctrl+C，再取这里。"""
    ref = await _running(session, thread_id, principal, browser)
    try:
        return BrowserClipboard(text=await browser.daemon.clipboard_get(ref))
    except DaemonError as exc:
        raise BrowserNotRunning(f"复制失败：{exc}") from exc


# ---------------------------------------------------------------------------- 我的登录


@router.get("/browser/logins", response_model=BrowserLoginList)
async def list_logins(principal: PrincipalDep, browser: BrowserDep) -> BrowserLoginList:
    items = await browser.vault.sites(principal.uid)
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
    if not await browser.vault.forget(principal.uid, target):
        raise NotFound("没有这个站点的登录", site=site)
    await browser.hosts.forget(principal.uid, target)
    return Response(status_code=204)


# ---------------------------------------------------------------------------- 画面


@ws_router.websocket("/threads/{thread_id}/browser/view")
async def browser_view(websocket: WebSocket, thread_id: UUID) -> None:
    """只给会话的创建人；浏览器没在运行就带着原因关掉，不为看画面建沙箱；连着的时候算沙箱的活动。"""
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
        subprotocols = list(websocket.scope.get("subprotocols") or [])
        await live.connect(websocket, target, subprotocols=subprotocols)
    finally:
        await redis.aclose()
