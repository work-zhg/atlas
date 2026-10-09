"""文件模板：所有角色可查看（file_template:view），平台管理员维护（file_template:manage）。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..identity import Principal
from ..templates.file_service import FileTemplateService
from .deps import SessionDep, SettingsDep, require
from .views import file_template_view, file_version_view, user_names

router = APIRouter(prefix="/api/v1/file-templates", tags=["file-templates"])

VIEW = require("file_template:view")
MANAGE = require("file_template:manage")


class FileTemplateBody(BaseModel):
    name: str | None = None
    icon: str | None = None
    description: str | None = None
    usage: str | None = None
    version: int | None = None


async def _detail(svc: FileTemplateService, uuid: UUID) -> dict[str, Any]:
    t = await svc.get(uuid)
    cur = await svc.current(t)
    versions = await svc.versions(t.uuid)
    names = await user_names(svc.session, {v.uploaded_by for v in versions} | {t.updated_by})
    out = file_template_view(t, cur)
    out["versions"] = [
        {**file_version_view(v), "uploaded_by_name": names.get(str(v.uploaded_by))}
        for v in versions
    ]
    out["references"] = await svc.references(t.uuid)
    out["content"] = cur.content if cur else None
    return out


@router.get("")
async def list_templates(
    session: SessionDep,
    p: Principal = VIEW,
    usage: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    svc = FileTemplateService(session, p.uuid)
    out = []
    for t in await svc.list(usage=usage, status=status, q=q):
        out.append(
            {
                **file_template_view(t, await svc.current(t)),
                "reference_count": len(await svc.references(t.uuid)),
            }
        )
    return out


@router.post("", status_code=201)
async def create(
    body: FileTemplateBody, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any]:
    svc = FileTemplateService(session, p.uuid)
    t = await svc.create(body.model_dump(exclude_unset=True))
    return await _detail(svc, t.uuid)


@router.get("/{tid}")
async def detail(tid: UUID, session: SessionDep, p: Principal = VIEW) -> dict[str, Any]:
    return await _detail(FileTemplateService(session, p.uuid), tid)


@router.patch("/{tid}")
async def update(
    tid: UUID, body: FileTemplateBody, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any]:
    svc = FileTemplateService(session, p.uuid)
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    await svc.update(tid, data, version)
    return await _detail(svc, tid)


@router.post("/{tid}/enable")
async def enable(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any]:
    svc = FileTemplateService(session, p.uuid)
    await svc.set_status(tid, True)
    return await _detail(svc, tid)


@router.post("/{tid}/disable")
async def disable(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any]:
    svc = FileTemplateService(session, p.uuid)
    await svc.set_status(tid, False)
    return await _detail(svc, tid)


@router.delete("/{tid}")
async def delete(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, bool]:
    await FileTemplateService(session, p.uuid).delete(tid)
    return {"ok": True}


@router.post("/{tid}/versions", status_code=201)
async def upload(
    tid: UUID,
    session: SessionDep,
    settings: SettingsDep,
    file: UploadFile = File(...),
    change_note: str = Form(""),
    version: int | None = Form(None),
    p: Principal = MANAGE,
) -> dict[str, Any]:
    svc = FileTemplateService(session, p.uuid)
    # 多读 1 字节即可判断超限，不把超大文件整个读进内存
    raw = await file.read(settings.file_template_max_bytes + 1)
    await svc.upload(
        tid, file.filename or "", raw, change_note, version, settings.file_template_max_bytes
    )
    return await _detail(svc, tid)


@router.get("/{tid}/versions/{no}")
async def version(tid: UUID, no: int, session: SessionDep, p: Principal = VIEW) -> dict[str, Any]:
    v = await FileTemplateService(session, p.uuid).version(tid, version_no=no)
    return file_version_view(v, content=True)


@router.get("/{tid}/versions/{no}/download")
async def download(tid: UUID, no: int, session: SessionDep, p: Principal = VIEW) -> Response:
    v = await FileTemplateService(session, p.uuid).version(tid, version_no=no)
    return Response(
        v.content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(v.file_name)}"},
    )
