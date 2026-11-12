"""工作空间：一个人的全部文件（{user}/files/）。路径一律相对 files/，根目录是空串。

★ 只看自己的：前缀按当前主体取，没有「看别人的」入口（管理员也没有）。
★ 会话的工作目录建会话时定下、之后不变：有会话在用的目录不能改名、移动；正在运行的不能删
  （services/workspace.py）。
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from ...auth.authz import PrincipalDep
from ...deps import RedisDep, SessionDep, SettingsDep
from ...runtime_config import WorkspaceLimits
from ...schemas.thread import PreviewSessionOut
from ...schemas.workspace import (
    WorkspaceDirOut,
    WorkspaceFolderIn,
    WorkspaceMoveIn,
    WorkspaceOut,
    WorkspacePathOut,
    WorkspaceRefsOut,
    WorkspaceSearchOut,
)
from ...services.preview import PreviewService
from ...services.workspace import UploadTooLarge, WorkspaceService

router = APIRouter(prefix="/workspace", tags=["workspace"])

PathQ = Annotated[str, Query(max_length=1024)]


async def workspace_limits(request: Request) -> WorkspaceLimits:
    """运行期配置里的上传上限、存储配额（每次读，改了不用重启）。"""
    return await request.app.state.runtime_config.workspace_limits()  # type: ignore[no-any-return]


async def read_upload(request: Request, limit: int) -> bytes:
    """读上传的请求体：超过上限就停，不把超大的请求体整个读进内存。limit 为 0 = 不限。"""
    declared = request.headers.get("content-length", "")
    if limit and declared.isdigit() and int(declared) > limit:
        raise UploadTooLarge(f"文件超过单个 {limit // 1024 // 1024} MB 的上限")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if limit and len(buf) > limit:
            raise UploadTooLarge(f"文件超过单个 {limit // 1024 // 1024} MB 的上限")
    return bytes(buf)


@router.get("", response_model=WorkspaceOut)
async def overview(
    request: Request, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> WorkspaceOut:
    """占用、配额、单个文件上传上限。"""
    limits = await workspace_limits(request)
    return await WorkspaceService(session, settings).overview(principal.uid, limits)


@router.get("/entries", response_model=WorkspaceDirOut)
async def list_entries(
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    dir: PathQ = "",
    hidden: bool = False,
) -> WorkspaceDirOut:
    """列一层：文件夹（文件数、大小）和文件。hidden = 也列点开头的（会话的内部记录 .atlas）。"""
    return await WorkspaceService(session, settings).list_dir(principal.uid, dir, hidden=hidden)


@router.get("/search", response_model=WorkspaceSearchOut)
async def search(
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    hidden: bool = False,
) -> WorkspaceSearchOut:
    """按名称找文件和文件夹（不搜内容）。"""
    return await WorkspaceService(session, settings).search(principal.uid, q, hidden=hidden)


@router.get("/refs", response_model=WorkspaceRefsOut)
async def refs(
    session: SessionDep, settings: SettingsDep, principal: PrincipalDep, path: PathQ
) -> WorkspaceRefsOut:
    """把这个目录（或它下面的目录）当工作目录的会话：改名、删除前给用户看。"""
    return await WorkspaceService(session, settings).refs(principal.uid, path)


@router.put("/files", status_code=status.HTTP_201_CREATED, response_model=WorkspacePathOut)
async def upload(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> WorkspacePathOut:
    """把请求体原样写成一个文件（覆盖同名；父目录不存在就一并出现）。"""
    limits = await workspace_limits(request)
    data = await read_upload(request, limits.upload_max)
    size = await WorkspaceService(session, settings).put(principal.uid, path, data, limits)
    return WorkspacePathOut(path=path.strip("/"), size=size)


@router.get("/files/download")
async def download(
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> StreamingResponse:
    body, size = await WorkspaceService(session, settings).open(principal.uid, path)
    name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    return StreamingResponse(
        iterate_in_threadpool(body.iter_chunks(64 * 1024)),
        media_type="application/octet-stream",
        headers={
            # RFC 5987：中文文件名要走 filename*，否则浏览器拿到的是乱码
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            "Content-Length": str(size),
        },
    )


@router.get("/zip")
async def download_zip(
    session: SessionDep, settings: SettingsDep, principal: PrincipalDep, path: PathQ = ""
) -> StreamingResponse:
    """把一个文件夹打包成 zip 下载（流式，不含隐藏文件）。path 为空 = 整个工作空间。"""
    chunks = await WorkspaceService(session, settings).zip(principal.uid, path)
    name = (path.strip("/").rsplit("/", 1)[-1] or "工作空间") + ".zip"
    return StreamingResponse(
        iterate_in_threadpool(chunks),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.post("/folders", status_code=status.HTTP_201_CREATED, response_model=WorkspacePathOut)
async def make_folder(
    payload: WorkspaceFolderIn, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> WorkspacePathOut:
    path = await WorkspaceService(session, settings).mkdir(principal.uid, payload.path)
    return WorkspacePathOut(path=path)


@router.post("/move", response_model=WorkspacePathOut)
async def move(
    payload: WorkspaceMoveIn, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> WorkspacePathOut:
    """改名 / 移动一个文件或文件夹。有会话以它为工作目录的文件夹不能动（409）。"""
    path = await WorkspaceService(session, settings).move(principal.uid, payload.src, payload.dst)
    return WorkspacePathOut(path=path)


@router.delete("/entries", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    session: SessionDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> Response:
    """删一个文件或文件夹（不可恢复）。正在运行的会话的工作目录不能删（409）。"""
    await WorkspaceService(session, settings).delete(principal.uid, path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/preview", response_model=PreviewSessionOut)
async def create_preview(
    request: Request, redis: RedisDep, settings: SettingsDep, principal: PrincipalDep
) -> PreviewSessionOut:
    """签发预览令牌，根是这个人的全部文件。站点在 GET /v1/previews/{token}/… 下。"""
    service = PreviewService(redis, settings)
    token, expires_at = await service.issue_files(principal.uid)
    return PreviewSessionOut(
        base_url=service.base_url(token, request_base=str(request.base_url)),
        expires_at=expires_at,
    )
