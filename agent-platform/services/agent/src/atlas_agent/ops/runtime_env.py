"""运行环境契约在 Atlas 的实现（c4-model/agent/code-ops.html · RuntimeEnvOps）：沙箱模板。

    GET   /ops/v1/sandbox-templates                   列出（不含 yaml）
    GET   /ops/v1/sandbox-templates/{code}            详情：yaml、版本、发布、漂移
    POST  /ops/v1/sandbox-templates                   新建（未发布）
    PUT   /ops/v1/sandbox-templates/{code}            修订：只改库、版本加一
    POST  /ops/v1/sandbox-templates/{code}/publish    发布：写 ConfigMap、滚动重启

★ 只给运营中心（先验运营接口的签名）：模板决定 Pod 的 securityContext，能改模板就能建特权 Pod，
  而且影响所有租户。原来的 /v1/sandbox-templates（按 sandbox:manage 判）移到这里；租户里不再有
  平台级的操作码。提交前都经 TemplatePolicy 校验，不合规的直接 422。
★ 沙箱的生命周期日志属于租户（sandbox_event），不在运营接口里给。
★ 新增模板 code 要先部署一个它绑定的 lifecycle server（部署脚本做）：OpenSandbox 1.1.1 的
  创建请求不能选模板。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from atlas_opsapi import OperatorStamp
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..db.models import SandboxTemplate
from ..deps import SessionDep
from ..errors import AppError, Conflict, DependencyUnavailable, NotFound
from ..sandbox.base.lifecycle.kube import KubeError
from ..sandbox.base.lifecycle.templates import (
    SandboxTemplates,
    TemplateConflict,
    TemplateNotFound,
    TemplatePublisher,
    TemplateRejected,
)

router = APIRouter(prefix="/sandbox-templates", tags=["ops"])


async def _operator(request: Request) -> OperatorStamp:
    return await request.app.state.ops_guard.verify(request)  # type: ignore[no-any-return]


Operator = Annotated[OperatorStamp, Depends(_operator)]

#: 运营中心自动发起、没有运营账号时记的操作人
_NOBODY = UUID(int=0)


def _by(op: OperatorStamp) -> UUID:
    return op.operator_uuid or _NOBODY


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


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    session: SessionDep, _: Operator, publisher: Publisher
) -> list[TemplateOut]:
    return [_out(t) for t in await SandboxTemplates(session, publisher).list()]


@router.get("/{code}", response_model=TemplateDetailOut)
async def get_template(
    code: str, session: SessionDep, _: Operator, publisher: Publisher
) -> TemplateDetailOut:
    templates = SandboxTemplates(session, publisher)
    template: SandboxTemplate = await _call(templates.get(code))
    try:
        drifted = await templates.drifted(code)
    except KubeError:
        drifted = None  # 查不到 server 上的内容不挡着看模板
    return _detail(template, drifted)


@router.post("", response_model=TemplateDetailOut, status_code=201)
async def create_template(
    body: TemplateIn, session: SessionDep, op: Operator, publisher: Publisher
) -> TemplateDetailOut:
    template: SandboxTemplate = await _call(
        SandboxTemplates(session, publisher).create(
            body.code, body.description, body.server, body.yaml, by=_by(op)
        )
    )
    await session.commit()
    await session.refresh(template)
    return _detail(template, None)


@router.put("/{code}", response_model=TemplateDetailOut)
async def revise_template(
    code: str, body: RevisionIn, session: SessionDep, op: Operator, publisher: Publisher
) -> TemplateDetailOut:
    templates = SandboxTemplates(session, publisher)
    await _call(templates.revise(code, body.yaml, body.description, by=_by(op)))
    await session.commit()
    template = await templates.get(code)
    await session.refresh(template)
    return _detail(template, None)


@router.post("/{code}/publish", response_model=TemplateDetailOut)
async def publish_template(
    code: str, session: SessionDep, op: Operator, publisher: Publisher
) -> TemplateDetailOut:
    template: SandboxTemplate = await _call(
        SandboxTemplates(session, publisher).publish(code, by=_by(op))
    )
    await session.commit()
    await session.refresh(template)
    return _detail(template, False)
