"""沙箱模板的管理接口（c4-model/agent/sandbox/code-sandbox-base-lifecycle.html「管理模板」「发布模板」）。

    GET   /sandbox-templates                   列出（不含 yaml）                      sandbox:manage
    GET   /sandbox-templates/{code}            详情：yaml、版本、发布、漂移          sandbox:manage
    POST  /sandbox-templates                   新建（未发布）                         sandbox:manage
    PUT   /sandbox-templates/{code}            修订：只改库、版本加一                sandbox:manage
    POST  /sandbox-templates/{code}/publish    发布：写 ConfigMap、滚动重启          sandbox:manage
    GET   /sandboxes/{sandbox_uuid}/events     沙箱的生命周期（删了也查得到）        sandbox:manage

★ 只有平台管理员能改：模板决定 Pod 的 securityContext，能改模板就能建特权 Pod。提交前都经
  TemplatePolicy 校验，不合规的直接 422。
★ 新增模板 code 要先部署一个它绑定的 lifecycle server（部署脚本做）：OpenSandbox 1.1.1 的
  创建请求不能选模板。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ...auth.authz import require
from ...auth.principal import Principal
from ...db.models import SandboxTemplate
from ...deps import SessionDep
from ...errors import AppError, Conflict, DependencyUnavailable, NotFound
from ...repositories.sandbox import SandboxEventLog
from ...sandbox.base.lifecycle.kube import KubeError
from ...sandbox.base.lifecycle.templates import (
    SandboxTemplates,
    TemplateConflict,
    TemplateNotFound,
    TemplatePublisher,
    TemplateRejected,
)

router = APIRouter(tags=["sandbox"])

SandboxAdmin = Annotated[Principal, Depends(require("sandbox:manage"))]


class InvalidTemplate(AppError):
    status_code = 422
    kind = "invalid_template"


class TemplateIn(BaseModel):
    code: str = Field(min_length=1, max_length=63)
    description: str = Field(default="", max_length=2000)
    server: str = Field(min_length=1, max_length=63)
    yaml: str = Field(min_length=1, max_length=64_000)


class RevisionIn(BaseModel):
    yaml: str = Field(min_length=1, max_length=64_000)
    description: str | None = Field(default=None, max_length=2000)


class TemplateOut(BaseModel):
    template_uuid: UUID
    code: str
    description: str
    server: str
    version: int
    published_version: int | None
    published_at: datetime | None
    updated_by: UUID


class TemplateDetailOut(TemplateOut):
    yaml: str
    published_sha: str | None
    #: server 上生效的内容和已发布的对不上（被手工改过）；查不到为 null
    drifted: bool | None = None


class SandboxEventOut(BaseModel):
    event_uuid: UUID
    sandbox_uuid: UUID | None
    external_id: str | None
    event_type: str
    state: str | None
    reason: str | None
    detail: dict[str, Any]
    occurred_at: datetime


def _publisher(request: Request) -> TemplatePublisher | None:
    return getattr(request.app.state, "template_publisher", None)


Publisher = Annotated[TemplatePublisher | None, Depends(_publisher)]


def _out(t: SandboxTemplate) -> TemplateOut:
    return TemplateOut(
        template_uuid=t.template_uuid,
        code=t.code,
        description=t.description,
        server=t.server,
        version=t.version,
        published_version=t.published_version,
        published_at=t.published_at,
        updated_by=t.updated_by_uuid,
    )


def _detail(t: SandboxTemplate, drifted: bool | None) -> TemplateDetailOut:
    return TemplateDetailOut(
        **_out(t).model_dump(), yaml=t.yaml, published_sha=t.published_sha, drifted=drifted
    )


async def _call[T](coro: Any) -> T:
    try:
        return await coro
    except TemplateRejected as exc:
        raise InvalidTemplate(str(exc)) from exc
    except TemplateNotFound as exc:
        raise NotFound(str(exc)) from exc
    except TemplateConflict as exc:
        raise Conflict(str(exc)) from exc
    except KubeError as exc:
        raise DependencyUnavailable(f"发布失败：{exc}") from exc


@router.get("/sandbox-templates", response_model=list[TemplateOut])
async def list_templates(
    session: SessionDep, _: SandboxAdmin, publisher: Publisher
) -> list[TemplateOut]:
    return [_out(t) for t in await SandboxTemplates(session, publisher).list()]


@router.get("/sandbox-templates/{code}", response_model=TemplateDetailOut)
async def get_template(
    code: str, session: SessionDep, _: SandboxAdmin, publisher: Publisher
) -> TemplateDetailOut:
    templates = SandboxTemplates(session, publisher)
    template: SandboxTemplate = await _call(templates.get(code))
    try:
        drifted = await templates.drifted(code)
    except KubeError:
        drifted = None  # 查不到 server 上的内容不挡着看模板
    return _detail(template, drifted)


@router.post("/sandbox-templates", response_model=TemplateDetailOut, status_code=201)
async def create_template(
    body: TemplateIn, session: SessionDep, admin: SandboxAdmin, publisher: Publisher
) -> TemplateDetailOut:
    template: SandboxTemplate = await _call(
        SandboxTemplates(session, publisher).create(
            body.code, body.description, body.server, body.yaml, by=admin.uid
        )
    )
    await session.commit()
    await session.refresh(template)
    return _detail(template, None)


@router.put("/sandbox-templates/{code}", response_model=TemplateDetailOut)
async def revise_template(
    code: str, body: RevisionIn, session: SessionDep, admin: SandboxAdmin, publisher: Publisher
) -> TemplateDetailOut:
    templates = SandboxTemplates(session, publisher)
    await _call(templates.revise(code, body.yaml, body.description, by=admin.uid))
    await session.commit()
    template = await templates.get(code)
    await session.refresh(template)
    return _detail(template, None)


@router.post("/sandbox-templates/{code}/publish", response_model=TemplateDetailOut)
async def publish_template(
    code: str, session: SessionDep, admin: SandboxAdmin, publisher: Publisher
) -> TemplateDetailOut:
    template: SandboxTemplate = await _call(
        SandboxTemplates(session, publisher).publish(code, by=admin.uid)
    )
    await session.commit()
    await session.refresh(template)
    return _detail(template, False)


@router.get("/sandboxes/{sandbox_uuid}/events", response_model=list[SandboxEventOut])
async def sandbox_events(
    sandbox_uuid: UUID, session: SessionDep, _: SandboxAdmin
) -> list[SandboxEventOut]:
    return [
        SandboxEventOut(
            event_uuid=e.event_uuid,
            sandbox_uuid=e.sandbox_uuid,
            external_id=e.external_id,
            event_type=e.event_type,
            state=e.state,
            reason=e.reason,
            detail=e.detail,
            occurred_at=e.occurred_at,
        )
        for e in await SandboxEventLog(session).history(sandbox_uuid)
    ]
