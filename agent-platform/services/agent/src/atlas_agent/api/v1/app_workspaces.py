"""应用工作空间（c4-model/agent/code-workspace-app.html）：业务应用为自己的一件事要的目录。

    POST   /v1/app-workspaces                       申请（按应用给的编号幂等）
    DELETE /v1/app-workspaces/{编号}                 释放：删目录、标已释放
    GET    /v1/app-workspaces/{编号}/files?dir=      列一层
    PUT    /v1/app-workspaces/{编号}/files?path=     上传（请求体原样写成一个文件）
    GET    /v1/app-workspaces/{编号}/files/download  下载
    POST   /v1/app-workspaces/{编号}/files/stat      查哪些文件不在

★ 只认登记它的应用（X-Caller-App）和租户；人直接调（网关来的）一律当不存在。谁能看、谁能传由应用判。
★ 文件直连对象存储，路径相对空间的根，规则和会话的工作区相同（越界、隐藏目录拒绝）。
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from ...app_workspaces import AppWorkspaces
from ...auth.authz import PrincipalDep
from ...deps import SessionDep, SettingsDep
from ...errors import AppError, Conflict, NotFound
from ...providers.filesystem import PathEscape
from ...schemas.workspace import (
    AppWorkspaceIn,
    AppWorkspaceOut,
    AppWorkspaceStatIn,
    AppWorkspaceStatOut,
    WorkspaceDirOut,
    WorkspacePathOut,
)
from .workspace import read_upload, workspace_limits

router = APIRouter(prefix="/app-workspaces", tags=["app-workspaces"])

PathQ = Annotated[str, Query(min_length=1, max_length=1024)]


def _out(space) -> AppWorkspaceOut:  # type: ignore[no-untyped-def]
    return AppWorkspaceOut(
        space_uuid=space.space_uuid,
        app_code=space.app_code,
        title=space.title,
        status=space.status,
        created_at=space.created_at.isoformat(),
    )


@router.post("", response_model=AppWorkspaceOut, status_code=status.HTTP_201_CREATED)
async def open_workspace(
    payload: AppWorkspaceIn, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> Response:
    """第一次申请 201，同一个编号再申请 200（拿到的是同一个）。"""
    space, created = await AppWorkspaces(session, settings).open(
        payload.space_uuid, payload.title, app=principal.app, actor=principal.uid
    )
    return JSONResponse(
        _out(space).model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.delete("/{space_uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def release_workspace(
    space_uuid: UUID, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> Response:
    """释放过的、没有的也回 204：应用清孤儿时会重试。"""
    await AppWorkspaces(session, settings).release(space_uuid, app=principal.app)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{space_uuid}/files", response_model=WorkspaceDirOut)
async def list_files(
    space_uuid: UUID,
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    dir: Annotated[str, Query(max_length=1024)] = "",
) -> WorkspaceDirOut:
    files = await AppWorkspaces(session, settings).files(space_uuid, app=principal.app)
    try:
        listed = await asyncio.to_thread(files.list_dir, dir)
    except PathEscape as exc:
        raise AppError(str(exc), dir=dir) from exc
    return WorkspaceDirOut.model_validate(listed)


@router.post("/{space_uuid}/files/stat", response_model=AppWorkspaceStatOut)
async def stat_files(
    space_uuid: UUID,
    payload: AppWorkspaceStatIn,
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> AppWorkspaceStatOut:
    files = await AppWorkspaces(session, settings).files(space_uuid, app=principal.app)

    def missing() -> list[str]:
        out = []
        for path in payload.paths:
            try:
                present = files.is_file(path)
            except PathEscape:
                present = False
            if not present:
                out.append(path)
        return out

    return AppWorkspaceStatOut(missing=await asyncio.to_thread(missing))


@router.put(
    "/{space_uuid}/files", status_code=status.HTTP_201_CREATED, response_model=WorkspacePathOut
)
async def upload(
    space_uuid: UUID,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    path: PathQ,
) -> WorkspacePathOut:
    """把请求体原样写成一个文件（覆盖同名）。单个文件的上限和工作空间相同。"""
    files = await AppWorkspaces(session, settings).files(space_uuid, app=principal.app)
    limits = await workspace_limits(request)
    data = await read_upload(request, limits.upload_max)
    try:
        size = await files.put(path, data)
    except PathEscape as exc:
        raise AppError(str(exc), path=path) from exc
    except FileExistsError as exc:
        raise Conflict(str(exc), path=path) from exc
    return WorkspacePathOut(path=files.rel(path), size=size)


@router.get("/{space_uuid}/files/download")
async def download(
    space_uuid: UUID,
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    path: PathQ,
) -> StreamingResponse:
    files = await AppWorkspaces(session, settings).files(space_uuid, app=principal.app)
    try:
        body, size = await asyncio.to_thread(files.open, path)
    except PathEscape as exc:
        raise AppError(str(exc), path=path) from exc
    except Exception as exc:  # NoSuchKey 等
        raise NotFound(f"空间里没有文件 {path}", path=path) from exc
    name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    return StreamingResponse(
        iterate_in_threadpool(body.iter_chunks(64 * 1024)),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Content-Length": str(size),
        },
    )
