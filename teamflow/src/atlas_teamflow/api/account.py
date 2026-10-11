"""当前用户的 Gitee 账号绑定（产物仓库与 Agent 提交 · §7）。

绑定后按团队身份把本人加为各项目产物仓库的协作者，本地 clone / pull 用自己的 Gitee 凭据。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, select

from ..audit import record
from ..db.models import GitAccount, RepoCollaborator
from ..errors import Conflict, Invalid, TFError
from ..gitee_oauth import GiteeOAuth
from ..process.repository import sync_team_collaborators
from .deps import PrincipalDep, SessionDep, SettingsDep, UCDep
from .views import iso

router = APIRouter(prefix="/api/v1/me/git-account", tags=["account"])
log = logging.getLogger("atlas_teamflow.account")

STATE_COOKIE = "tf_gitee_state"


class CallbackBody(BaseModel):
    code: str
    state: str


def _oauth(request: Request) -> GiteeOAuth:
    return request.app.state.gitee_oauth  # type: ignore[no-any-return]


async def _view(request: Request, session: SessionDep, user: UUID) -> dict[str, Any]:
    a = await session.scalar(select(GitAccount).where(GitAccount.user_uuid == user))
    repos = [
        {
            "login": r.login,
            "permission": r.permission,
            "error": r.error,
            "project_id": str(r.project_uuid),
        }
        for r in await session.scalars(
            select(RepoCollaborator).where(RepoCollaborator.user_uuid == user)
        )
    ]
    return {
        "oauth_enabled": _oauth(request).enabled,
        "account": {
            "provider": a.provider,
            "login": a.login,
            "name": a.name,
            "bound_at": iso(a.bound_at),
        }
        if a
        else None,
        "repositories": repos,
    }


async def _sync_my_teams(request: Request, session: SessionDep, uc: UCDep, user: UUID) -> None:
    host = request.app.state.repo
    try:
        teams = await uc.accessible_teams(user, "WRITE")
    except TFError as exc:  # 用户中心暂不可用：对账会补齐
        log.warning("读取用户 %s 的团队失败，协作者稍后对账补齐：%s", user, exc.message)
        return
    for team in teams:
        await sync_team_collaborators(session, uc, host, UUID(team))


@router.get("")
async def get_account(
    request: Request, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    return await _view(request, session, principal.uuid)


@router.post("/oauth")
async def start_oauth(
    request: Request, response: Response, principal: PrincipalDep, settings: SettingsDep
) -> dict[str, str]:
    oauth = _oauth(request)
    if not oauth.enabled:
        raise Conflict("GITEE_OAUTH_DISABLED", "未配置 Gitee OAuth 应用，请联系平台管理员")
    state = secrets.token_urlsafe(24)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"url": oauth.authorize_url(state)}


@router.post("/oauth/callback")
async def oauth_callback(
    body: CallbackBody,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    session: SessionDep,
    uc: UCDep,
) -> dict[str, Any]:
    expected = request.cookies.get(STATE_COOKIE)
    response.delete_cookie(STATE_COOKIE, path="/")
    if not expected or not secrets.compare_digest(expected, body.state):
        raise Invalid("GITEE_OAUTH_STATE", "绑定请求已失效，请重新发起绑定")
    user = await _oauth(request).exchange(body.code)
    taken = await session.scalar(
        select(GitAccount).where(
            GitAccount.provider == "gitee", GitAccount.provider_user_id == user.id
        )
    )
    if taken and taken.user_uuid != principal.uuid:
        raise Conflict("GIT_ACCOUNT_TAKEN", f"Gitee 账号 {user.login} 已被其他成员绑定")
    a = await session.scalar(select(GitAccount).where(GitAccount.user_uuid == principal.uuid))
    if a is None:
        a = GitAccount(
            user_uuid=principal.uuid, provider="gitee", login=user.login, provider_user_id=user.id
        )
        session.add(a)
    a.login, a.provider_user_id, a.name = user.login, user.id, user.name
    await session.flush()
    record(
        session,
        principal.uuid,
        "git_account.bound",
        "user",
        principal.uuid,
        principal.user.name,
        login=user.login,
    )
    await _sync_my_teams(request, session, uc, principal.uuid)
    return await _view(request, session, principal.uuid)


@router.delete("")
async def unbind(
    request: Request, principal: PrincipalDep, session: SessionDep, uc: UCDep
) -> dict[str, Any]:
    a = await session.scalar(select(GitAccount).where(GitAccount.user_uuid == principal.uuid))
    if a is not None:
        await session.execute(delete(GitAccount).where(GitAccount.id == a.id))
        await session.flush()
        record(
            session,
            principal.uuid,
            "git_account.unbound",
            "user",
            principal.uuid,
            principal.user.name,
            login=a.login,
        )
        await _sync_my_teams(request, session, uc, principal.uuid)  # 解绑 → 从各仓库移除
    return await _view(request, session, principal.uuid)
