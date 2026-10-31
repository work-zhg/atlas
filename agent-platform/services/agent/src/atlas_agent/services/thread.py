"""会话与消息服务（文档 §11.2）。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_engine.contracts import SandboxUnavailable
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.agent_access import AgentAccess
from ..auth.principal import Principal
from ..auth.uc import UCClient
from ..config import Settings
from ..db.models import Agent, AgentVersion, Message, Thread
from ..errors import AppError, BadCursor, Conflict, DependencyUnavailable, Forbidden, NotFound
from ..pagination import InvalidCursor, decode_cursor, encode_cursor
from ..providers.filesystem import make_workspace
from ..providers.filesystem.oss import PathEscape, WorkspaceListing
from ..providers.filesystem.skill_copy import resolve_skills, seed_session_skills
from ..providers.opensandbox import BENCH, AcpProvisioner, SandboxProvisioner
from ..providers.sandbox import SandboxNotRunning, SandboxProfileDenied, exec_in_running
from ..redisx import make_redis
from ..repositories.agent import AgentRepository
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..schemas.agent import AgentSpecIn
from ..schemas.thread import (
    MessageListOut,
    MessageOut,
    SandboxExecIn,
    SandboxExecOut,
    ThreadApprovalOut,
    ThreadCreate,
    ThreadListOut,
    ThreadOut,
    ThreadUpdate,
    WorkspaceFilesOut,
)
from ..skills import skill_directory

if TYPE_CHECKING:
    from ..memory import MemoryRuntime


def _fold_assistant_turns(rows: list[Message]) -> list[MessageOut]:
    """把同一个 run 里的多条助手消息折成一条 —— 对外仍是「一轮一条」。

    ★ 为什么需要这一层投影。message 表存的是**执行的事实**：模型在一轮里
      说几次话就是几条 assistant 消息（每次调工具前一段），中间还夹着工具
      结果。那是模型侧需要的形状（domain/messages.py）。

      而前端的 MessageStream 假定「一轮一条助手消息，content 里的多个 text
      block 是分段」—— 末段当结论正常显示，前面的段弱化成过程性发言。不折
      的话，每条消息各自只有一段，于是每一段都被当成结论，过程性发言的弱化
      全部失效（视觉上是一堆并列的、带头像和时间戳的气泡）。

    ★ 顺便丢掉 tool_use block。前端的 blocksOf 只认 text，本来就会忽略它 ——
      这里显式滤掉是为了不把执行细节送到网线上（一次 write_file 的 input 可能
      是整个文件内容）。

    ★ 分页边界切开一轮时，那一轮会显示成两条。罕见（limit 默认 50）且无害，
      不值得为它引入跨页的状态。
    """
    ordered = list(reversed(rows))  # 入参是倒序（最新在前），折叠按时间正序做
    folded: list[MessageOut] = []
    for row in ordered:
        blocks = [
            block
            for block in (row.content if isinstance(row.content, list) else [])
            if isinstance(block, dict) and block.get("type") != "tool_use"
        ]
        previous = folded[-1] if folded else None
        mergeable = (
            previous is not None
            and row.role == "assistant"
            and previous.role == "assistant"
            # run_id 为 None 的历史消息不参与折叠：没有它就无法判断「同一轮」，
            # 而按相邻就折会把两轮的回答粘在一起。
            and row.run_id is not None
            and previous.run_id == row.run_id
        )
        if mergeable and previous is not None:
            folded[-1] = previous.model_copy(update={"content": [*previous.content, *blocks]})
            continue
        folded.append(
            MessageOut(
                id=row.id,
                thread_id=row.thread_id,
                run_id=row.run_id,
                role=row.role,  # type: ignore[arg-type]
                content=blocks,
                created_at=row.created_at,
            )
        )
    folded.reverse()  # 还原成倒序，契约不变
    return folded


def _to_out(
    thread: Thread,
    agent: Agent,
    *,
    subagent_thread_count: int = 0,
    active_run_id: UUID | None = None,
    last_run: dict[str, Any] | None = None,
    approval: ThreadApprovalOut | None = None,
) -> ThreadOut:
    return ThreadOut(
        id=thread.id,
        agent_id=agent.id,
        agent_slug=agent.slug,
        agent_name=agent.name,
        agent_avatar_key=agent.avatar_key,
        title=thread.title,
        title_source=thread.title_source,  # type: ignore[arg-type]
        status=thread.status,  # type: ignore[arg-type]
        parent_thread_id=thread.parent_thread_id,
        subagent_name=thread.subagent_name,
        git_profile=thread.git_profile,
        memory=thread.memory,
        approval_mode=thread.approval_mode,
        approval=approval,
        source_app=thread.source_app,
        subagent_thread_count=subagent_thread_count,
        active_run_id=active_run_id,
        last_run=last_run,
        message_count=thread.message_count,
        compact_count=thread.compact_count,
        latest_state=thread.latest_state,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _parse_cursor(cursor: str | None):
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except InvalidCursor as exc:
        raise BadCursor(str(exc)) from exc


logger = logging.getLogger(__name__)


class BadRequest(AppError):
    status_code = 400
    kind = "bad_request"


class SandboxNotRunningError(Conflict):
    kind = "sandbox_not_running"


#: 直接执行时随命令写进沙箱的文件合计上限（与工作区上传一致）
SANDBOX_EXEC_FILES_MAX = 10 * 1024 * 1024


class ThreadService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        uc: UCClient | None = None,
        memory: MemoryRuntime | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        #: 记忆（doc/memory-design.html）。None = 没接入记忆服务
        self._memory = memory
        self._repo = ThreadRepository(session)
        self._agents = AgentRepository(session)
        #: 谁能用这个 Agent（AtlasAgent 数据权限）。没接用户中心时不限制
        self._access = AgentAccess(uc)

    # ------------------------------------------------------------------ 读

    async def list(
        self, *, owner: UUID | None, status: str | None, cursor: str | None, limit: int
    ) -> ThreadListOut:
        rows = await self._repo.list(
            owner=owner, status=status, cursor=_parse_cursor(cursor), limit=limit + 1
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            encode_cursor(rows[-1][0].updated_at, rows[-1][0].id) if has_more and rows else None
        )
        runs = await RunRepository(self._session).latest_runs_of([t.id for t, _ in rows])
        return ThreadListOut(
            data=[_to_out(t, a, last_run=runs.get(t.id)) for t, a in rows],
            next_cursor=next_cursor,
        )

    async def get(self, thread_id: UUID) -> ThreadOut:
        thread, agent = await self._require(thread_id)
        # 只在详情上算：删除确认要显示会被一起删掉的子会话数（设计 §12）。
        return _to_out(
            thread,
            agent,
            subagent_thread_count=await self._repo.count_subagent_threads(thread_id),
            # 刷新后恢复事件流的入口（列表接口不算，避免 N+1）
            active_run_id=await RunRepository(self._session).active_run_of(thread_id),
            last_run=(await RunRepository(self._session).latest_runs_of([thread_id])).get(
                thread_id
            ),
            approval=await self._approval_of(thread, agent),
        )

    async def _approval_of(self, thread: Thread, agent: Agent) -> ThreadApprovalOut | None:
        """会话当前的审批模式与可选范围，取自智能体**当前版本**（下一条消息就用它）。"""
        if agent.current_version_id is None:
            return None
        version = await self._session.get(AgentVersion, agent.current_version_id)
        if version is None:
            return None
        parsed = AgentSpecIn.model_validate(version.spec)
        if parsed.kind != "native":
            return None
        approval = parsed.approval.to_engine()
        return ThreadApprovalOut(
            mode=approval.resolve(thread.approval_mode),
            default_mode=approval.default_mode,
            allowed_modes=list(approval.allowed_modes),
        )

    async def list_messages(
        self, thread_id: UUID, *, cursor: str | None, limit: int
    ) -> MessageListOut:
        await self._require(thread_id)
        rows = await self._repo.list_messages(
            thread_id, cursor=_parse_cursor(cursor), limit=limit + 1
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        return MessageListOut(data=_fold_assistant_turns(rows), next_cursor=next_cursor)

    # ------------------------------------------------------------------ 写

    async def create(
        self,
        payload: ThreadCreate,
        *,
        principal: Principal | None = None,
        user_id: UUID | None = None,
    ) -> ThreadOut:
        """principal 来自接口层；只给 user_id 的调用方（脚本、测试）按人直接操作处理。"""
        if principal is None:
            if user_id is None:
                raise TypeError("需要 principal 或 user_id")
            principal = Principal(uid=user_id)
        found = await self._agents.get(payload.agent_id)
        if found is None:
            raise NotFound(f"智能体 {payload.agent_id} 不存在", agent_id=str(payload.agent_id))
        agent, version = found
        # 能用 = 看得到（已启用 READ、草稿 WRITE 试跑）；看不到的与不存在一样 404（权限设计 §7.5）
        await self._access.require(principal, agent)
        user_id = principal.uid
        # 业务应用（TeamFlow、评测…）建的会话默认关闭用户记忆：节点会话不是人的私人对话。
        # 调用方可以显式指定（评测的跨会话记忆用例要打开）
        memory = payload.memory or ("off" if principal.app else "on")
        thread = await self._repo.create(
            agent_id=agent.id,
            title=payload.title,
            created_by=user_id,
            git_profile=payload.git_profile,
            source_app=principal.app,
            memory=memory,
        )
        await self._session.refresh(thread)
        await self._seed_skills(version, thread_id=thread.id, user_id=user_id)
        if memory == "on" and self._memory is not None:
            # 建会话时登记本会话用到的记忆实例（§4.1）。失败不阻塞会话：本会话暂时不用记忆，
            # 下一个 Run 开始时再登记
            await self._memory.register_thread(thread.id, user_id)
        return _to_out(thread, agent)

    async def _seed_skills(self, version: AgentVersion, *, thread_id: UUID, user_id: UUID) -> None:
        """把 agent 引用的技能拷进本会话的 skills 前缀。

        ★ 时机是**会话创建**，不是首次 run：用户对「新建会话」的延迟容忍度
          更高，而首轮对话的等待是最刺眼的。

        ★ 失败不吞。少拷几个文件就默默开始的话，表现是模型「照技能说的做了
          但脚本不存在」—— 比明确报错难查得多。异常冒泡会让整个请求事务
          回滚，于是**会话根本不会建出来**，而不是建出一个技能残缺的会话。

        没配对象存储时 make_workspace 返回 None —— 那是「没有文件能力」这个
        产品决定的一部分，技能一并不投送（模型那边文件工具也一个都不注册）。
        """
        spec = AgentSpecIn.model_validate(version.spec)
        if not spec.skills:
            return
        fs = make_workspace(self._settings, user_id, thread_id)
        if fs is None:
            logger.info("未配置对象存储，跳过技能投送 thread=%s", thread_id)
            return
        # ★ 拷贝只需要 (slug, version)；描述在每轮装配时取（那里才渲染给模型）。
        #   这里只过滤已下架的 —— 建会话不该因为拉描述而多一次失败点。
        metas, skipped = await resolve_skills(
            [s.to_engine() for s in spec.skills],
            skill_directory(self._settings),
            with_descriptions=False,
        )
        await seed_session_skills(fs, metas)
        if skipped:
            logger.warning("会话 %s 跳过了已下架的技能：%s", thread_id, skipped)
        logger.info("会话 %s 投送了 %d 个技能", thread_id, len(metas))

    async def update(self, thread_id: UUID, payload: ThreadUpdate) -> ThreadOut:
        thread, agent = await self._require(thread_id)
        if payload.title is not None:
            thread.title = payload.title
            # ★ 用户改过的标题永不被自动生成覆盖（决策 5）
            thread.title_source = "manual"
        if payload.status is not None:
            thread.status = payload.status
        await self._session.flush()
        await self._session.refresh(thread)
        return _to_out(thread, agent)

    async def delete(self, thread_id: UUID) -> None:
        thread, _ = await self._require(thread_id)
        await self._repo.delete(thread)
        # 沙箱级联：清理失败不该挡住会话删除（只记日志），残留由维护者的孤儿回收兜底
        redis = make_redis(self._settings)
        try:
            await AcpProvisioner(self._settings, redis).release(str(thread_id))
            if self._settings.sandbox_enabled:
                # 原生沙箱按工作区建：删的是工作区所有者时它才有沙箱
                await SandboxProvisioner(self._settings, redis).release(str(thread_id))
        finally:
            await redis.aclose()

    # ------------------------------------------------------------------ 工作区文件

    async def list_files(self, thread_id: UUID) -> WorkspaceFilesOut:
        """列工作区的真实内容，而不是靠 file.written 事件拼 —— acp 子智能体的 CLI
        在 Pod 里直接写挂载的工作区，平台侧没有任何事件。"""
        listing = await self._listing(thread_id)
        if listing is None:
            return WorkspaceFilesOut(configured=False, data=[])
        files, truncated = await asyncio.to_thread(listing.list)
        return WorkspaceFilesOut(configured=True, data=files, truncated=truncated)  # type: ignore[arg-type]

    async def open_file(self, thread_id: UUID, path: str) -> tuple[Any, int]:
        listing = await self._listing(thread_id)
        if listing is None:
            raise NotFound("没有配置对象存储，这个部署没有文件能力")
        try:
            return await asyncio.to_thread(listing.open, path)
        except PathEscape as exc:
            raise BadRequest(str(exc), path=path) from exc
        except Exception as exc:  # NoSuchKey 等
            raise NotFound(f"工作区里没有文件 {path}", path=path) from exc

    async def put_file(self, thread_id: UUID, path: str, data: bytes) -> int:
        listing = await self._listing(thread_id)
        if listing is None:
            raise NotFound("没有配置对象存储，这个部署没有文件能力")
        try:
            return await asyncio.to_thread(listing.put, path, data)
        except PathEscape as exc:
            raise BadRequest(str(exc), path=path) from exc

    # ------------------------------------------------------------------ 沙箱

    async def exec_in_sandbox(self, thread_id: UUID, payload: SandboxExecIn) -> SandboxExecOut:
        """在会话正在运行的**评测档**沙箱里执行一条命令，等它结束（评测原地校验）。

        不新建沙箱、只认评测档：见 providers/sandbox.py::exec_in_running。
        """
        thread, _ = await self._require(thread_id)
        if not self._settings.sandbox_enabled:
            raise SandboxNotRunningError("这个部署没有开启沙箱")
        files: list[tuple[str, bytes]] = []
        for f in payload.files:
            try:
                files.append((f.path, base64.b64decode(f.content_b64, validate=True)))
            except binascii.Error as exc:
                raise BadRequest("content_b64 不是合法的 base64", path=f.path) from exc
        if sum(len(data) for _, data in files) > SANDBOX_EXEC_FILES_MAX:
            raise BadRequest(f"文件合计超过 {SANDBOX_EXEC_FILES_MAX // 1024 // 1024} MB")
        settings = self._settings
        redis = make_redis(settings)
        try:
            logger.info("会话 %s 在评测档沙箱里直接执行命令", thread_id)
            result = await exec_in_running(
                SandboxProvisioner(settings, redis),
                str(thread.workspace_thread_id),
                user_id=str(thread.created_by),
                command=payload.command,
                profile=BENCH,
                cwd=payload.cwd,
                timeout=payload.timeout_s,
                files=files,
                output_limit=settings.sandbox_output_limit_bytes,
                poll_min_s=settings.sandbox_poll_min_s,
                poll_max_s=settings.sandbox_poll_max_s,
            )
        except SandboxNotRunning as exc:
            raise SandboxNotRunningError(str(exc), thread_id=str(thread_id)) from exc
        except SandboxProfileDenied as exc:
            raise Forbidden(str(exc), thread_id=str(thread_id)) from exc
        except SandboxUnavailable as exc:
            # 执行途中沙箱没了 / 连不上：校验没做成，不是校验没通过
            raise DependencyUnavailable(str(exc), thread_id=str(thread_id)) from exc
        finally:
            await redis.aclose()
        return SandboxExecOut(
            exit_code=result.exit_code, output=result.output, truncated=result.truncated
        )

    async def _listing(self, thread_id: UUID) -> WorkspaceListing | None:
        thread, _ = await self._require(thread_id)
        fs = make_workspace(
            self._settings,
            thread.created_by,
            thread.id,
            # ★ 子会话的工作区是父会话的（共享挂载）
            workspace_thread_id=thread.workspace_thread_id,
        )
        return None if fs is None else WorkspaceListing(fs)

    # ------------------------------------------------------------------ 内部

    async def _require(self, thread_id: UUID) -> tuple[Thread, Agent]:
        found = await self._repo.get(thread_id)
        if found is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))
        return found
