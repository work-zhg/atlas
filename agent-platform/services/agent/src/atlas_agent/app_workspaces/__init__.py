"""工作区 · 应用工作空间（c4-model/agent/code-workspace-app.html）。

业务应用为自己的一件事要的目录（TeamFlow 每条流程一个，叫流程空间），不属于任何人或数字员工：

    AppWorkspaces    唯一入口：按编号登记（幂等）、释放、建会话时校验能不能挂、给文件接口一个以空间
                     为根的文件区（直连对象存储）。所有方法都先核对调用方应用（租户由租户过滤管）
    AttachPolicy     能不能挂给一个新会话：调用方就是这个应用、空间没释放、智能体以数字员工为权限

★ 权限归应用：平台不认识团队和流程，谁能看、谁能传由应用判；平台只认调用方（X-Caller-App）是登记
  它的应用、租户一致。别的应用、别的租户拿到编号也用不了（看不到的与不存在一样 404）。
★ 只挂给以数字员工为权限的会话：以人为权限的会话共用这个人的沙箱，建时只挂了 {人}/，挂不进会话专属
  的目录（sandbox/design-workspace-subject.html 原理 3）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Agent, AppWorkspace
from ..domain.workspace_layout import app_space_prefix
from ..errors import Conflict, NotFound
from ..providers.filesystem import UserFiles, make_space_files

__all__ = ["AppWorkspaces", "AttachPolicy", "SpaceReleased"]


class SpaceReleased(Conflict):
    """空间已经释放：挂着它的会话之后不能再开新一轮，也不能再读写它。

    410 而不是 409：会话忙（409）是等一等能好的，这个永远好不了，调用方不该重试。
    """

    status_code = 410
    kind = "space_released"


class AttachPolicy:
    @staticmethod
    def check(space: AppWorkspace, *, app: str | None, agent: Agent) -> None:
        if space.app_code != app:
            raise NotFound("应用工作空间不存在", app_workspace=str(space.space_uuid))
        if space.released:
            raise SpaceReleased("应用工作空间已释放", app_workspace=str(space.space_uuid))
        if not agent.acts_as_itself:
            raise Conflict(
                "应用工作空间只能给以数字员工为权限的智能体用",
                app_workspace=str(space.space_uuid),
                agent_id=str(agent.agent_uuid),
            )


class AppWorkspaces:
    def __init__(self, session: AsyncSession, settings: Any) -> None:
        self._session = session
        self._settings = settings

    async def get(self, space_uuid: UUID, *, app: str | None) -> AppWorkspace:
        """调用方登记的那个；别的应用的、别的租户的与不存在一样 404。"""
        space = await self._session.scalar(
            select(AppWorkspace).where(AppWorkspace.space_uuid == space_uuid)
        )
        if space is None or app is None or space.app_code != app:
            raise NotFound("应用工作空间不存在", app_workspace=str(space_uuid))
        return space

    async def open(
        self, space_uuid: UUID, title: str, *, app: str | None, actor: UUID
    ) -> tuple[AppWorkspace, bool]:
        """登记 → (空间, 是不是这次新建的)。同一个编号重复调返回同一个；释放过的不能再用。

        编号已被别的应用或租户用了就拒绝（唯一约束兜底）。目录不用预先建：对象存储没有空目录。
        """
        if app is None:
            raise Conflict("只有业务应用能申请应用工作空间（请求头 X-Caller-App）")
        existing = await self._session.scalar(
            select(AppWorkspace).where(AppWorkspace.space_uuid == space_uuid)
        )
        if existing is not None:
            if existing.app_code != app:
                raise Conflict("这个编号已经被别的应用用了", app_workspace=str(space_uuid))
            if existing.released:
                raise SpaceReleased("应用工作空间已释放", app_workspace=str(space_uuid))
            return existing, False
        space = AppWorkspace(
            space_uuid=space_uuid,
            app_code=app,
            title=title,
            prefix=f"{app_space_prefix(app, space_uuid)}/",
            created_by_uuid=actor,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(space)
                await self._session.flush()
        except IntegrityError as exc:
            # 别的租户用了这个编号（租户过滤看不到它），或者并发的同一个请求先插进去了
            raced = await self._session.scalar(
                select(AppWorkspace).where(AppWorkspace.space_uuid == space_uuid)
            )
            if raced is not None and raced.app_code == app and not raced.released:
                return raced, False
            raise Conflict("这个编号已经被用了", app_workspace=str(space_uuid)) from exc
        await self._session.refresh(space)
        return space, True

    async def release(self, space_uuid: UUID, *, app: str | None) -> None:
        """先删目录再标已释放。已经释放过、或者没有这个编号：什么都不做（应用清孤儿时重试）。

        ★ 不看还有没有会话挂着它：挂着的会话之后开新一轮报「空间已释放」，不回落到别处。
        """
        try:
            space = await self.get(space_uuid, app=app)
        except NotFound:
            return
        if space.released:
            return
        files = make_space_files(self._settings, space.prefix.rstrip("/"))
        if files is not None:
            await asyncio.to_thread(files.clear)
        space.status = "released"
        space.released_at = datetime.now(UTC)
        await self._session.flush()

    async def attach(self, space_uuid: UUID, *, app: str | None, agent: Agent) -> AppWorkspace:
        """建会话时：能挂才返回空间，会话记下它的编号（沙箱建时挂到 /space）。"""
        space = await self.get(space_uuid, app=app)
        AttachPolicy.check(space, app=app, agent=agent)
        return space

    async def files(self, space_uuid: UUID, *, app: str | None) -> UserFiles:
        """以空间为根的文件区（直连对象存储），路径规则和会话的工作区相同。"""
        space = await self.get(space_uuid, app=app)
        if space.released:
            raise SpaceReleased("应用工作空间已释放", app_workspace=str(space_uuid))
        files = make_space_files(self._settings, space.prefix.rstrip("/"))
        if files is None:
            raise NotFound("没有配置对象存储，这个部署没有文件能力")
        return files

    async def released(self, space_uuid: UUID) -> bool:
        """会话开新一轮前查：挂着的空间释放了没有（按编号，不核对应用：会话已经核对过）。"""
        status = await self._session.scalar(
            select(AppWorkspace.status).where(AppWorkspace.space_uuid == space_uuid)
        )
        return status != "active"
