"""记忆管理（doc/memory-design.html §7）：用户查看、更正、撤销、恢复、删除自己的记忆。

    主题      GET /memories/topics · GET|PATCH|DELETE /memories/topics/{code}
    条目      GET|POST /memories/entries · GET|PATCH|DELETE /memories/entries/{number}
              POST /memories/entries/{number}/restore · POST /memories/moves
    修订      GET /memories/revisions · POST /memories/revisions/{id}/undo
    全部删除  DELETE /memories
    暂停      GET|PUT /memories/settings
    补记提示  GET /memories/backfills · POST /memories/backfills/seen

★ 这些操作只能由用户在管理界面完成，**绝不做成工具挂给 Agent**（§7）：Agent 和抽取模型都没有
  彻底删除的能力，只能取代或遗忘（§4.8）。
★ 作用对象永远是当前用户自己的实例（关联主键 = 请求的用户），不接受参数指定；记忆服务还会按
  用户中心数据权限再校验一次（§3.7）。
★ 影响常驻内容的操作在下一轮生效：每次改动都推进用户的 epoch，各会话据此刷新常驻块（§5.3）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select, update

from ...db.models import MemoryBackfill
from ...deps import CurrentUserDep, SessionDep
from ...errors import AppError, Conflict, DependencyUnavailable, Forbidden, NotFound
from ...memory import (
    SOURCE_USER,
    MemoryEntry,
    MemoryRejected,
    MemoryRuntime,
    MemoryTopic,
    MemoryUnavailable,
    ScopeRef,
)
from ...schemas.memory import (
    BackfillListOut,
    BackfillOut,
    BackfillSeenIn,
    EntryCreate,
    EntryDetailOut,
    EntryListOut,
    EntryOut,
    EntryRestoreIn,
    EntryUpdate,
    MemorySettingsIn,
    MemorySettingsOut,
    MoveIn,
    PurgeOut,
    RevisionListOut,
    RevisionOut,
    TopicDetailOut,
    TopicListOut,
    TopicOut,
    TopicPatch,
    WriteOut,
)

router = APIRouter(prefix="/memories", tags=["memories"])

#: 用户在管理界面的操作，出处里记这一条
_ORIGIN = {"via": "manage"}


class MemoryNotEnabled(AppError):
    status_code = 503
    kind = "memory_not_enabled"


def _runtime(request: Request) -> MemoryRuntime:
    """进程级的记忆运行时；没接入记忆服务就 503。

    ★ 明确报 503，不返回空列表：空列表会让「没接入」与「还没记下任何东西」看起来一模一样。
    """
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        raise MemoryNotEnabled("记忆功能未接入（MEMORY_BASE_URL 未配置）")
    return memory  # type: ignore[no-any-return]


RuntimeDep = Annotated[MemoryRuntime, Depends(_runtime)]


async def _call[T](memory: MemoryRuntime, user: UUID, fn: Callable[[], Awaitable[T]]) -> T:
    """调记忆服务，把它的拒绝翻译成本服务的错误。没登记过的用户先登记（第一次打开管理页时）。"""
    try:
        return await memory.call(user, fn)
    except MemoryUnavailable as exc:
        raise DependencyUnavailable("记忆服务暂时不可用，请稍后重试") from exc
    except MemoryRejected as exc:
        details = {"code": exc.code}
        if exc.status == 404:
            raise NotFound(exc.message, **details) from exc
        if exc.status == 403:
            raise Forbidden(exc.message, **details) from exc
        if exc.status == 409:
            raise Conflict(exc.message, **details) from exc
        raise AppError(exc.message, **details) from exc


def _topic_out(scope: ScopeRef, t: MemoryTopic, limit: int) -> TopicOut:
    return TopicOut(
        scope=scope.name,
        code=t.code,
        description=t.description,
        resident=t.resident,
        version=t.version,
        entry_count=t.entry_count,
        char_count=t.char_count,
        over_limit=t.char_count > limit,
    )


def _entry_out(e: MemoryEntry) -> EntryOut:
    return EntryOut(
        number=e.number,
        content=e.content,
        topic=e.topic,
        occurred_at=e.occurred_at,
        source=e.source,
        origin=e.origin,
        status=e.status,
        superseded_by=e.superseded_by,
        retired_at=e.retired_at,
        created_at=e.created_at,
    )


def _now() -> datetime:
    return datetime.now(UTC)


# ───────────────────────────── 主题


@router.get("/topics", response_model=TopicListOut)
async def list_topics(memory: RuntimeDep, user: CurrentUserDep) -> TopicListOut:
    scope = memory.user_scope(user)
    topics = await _call(memory, user, lambda: memory.client.catalog(scope, as_user=user))
    limit = memory.settings.memory_topic_max_chars
    return TopicListOut(data=[_topic_out(scope, t, limit) for t in topics], topic_max_chars=limit)


@router.get("/topics/{code}", response_model=TopicDetailOut)
async def get_topic(code: str, memory: RuntimeDep, user: CurrentUserDep) -> TopicDetailOut:
    scope = memory.user_scope(user)
    topic, entries = await _call(
        memory, user, lambda: memory.client.load(scope, code, as_user=user)
    )
    return TopicDetailOut(
        topic=_topic_out(scope, topic, memory.settings.memory_topic_max_chars),
        entries=[_entry_out(e) for e in entries],
    )


@router.patch("/topics/{code}", response_model=TopicOut)
async def update_topic(
    code: str, body: TopicPatch, memory: RuntimeDep, user: CurrentUserDep
) -> TopicOut:
    """修改主题的描述和常驻标记（§7「调整主题」）。"""
    scope = memory.user_scope(user)
    attrs = None
    if body.resident is not None:
        current, _ = await _call(
            memory, user, lambda: memory.client.load(scope, code, as_user=user)
        )
        attrs = {**current.attrs, "resident": body.resident}
    out = await _call(
        memory,
        user,
        lambda: memory.client.update_topic(
            scope, code, description=body.description, attrs=attrs, as_user=user
        ),
    )
    await memory.bump_epoch(user)
    return _topic_out(scope, MemoryTopic.of(out["topic"]), memory.settings.memory_topic_max_chars)


@router.delete("/topics/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(code: str, memory: RuntimeDep, user: CurrentUserDep) -> Response:
    """删除空主题（有条目的先移走或删除）。"""
    scope = memory.user_scope(user)
    await _call(memory, user, lambda: memory.client.delete_topic(scope, code, as_user=user))
    await memory.bump_epoch(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ───────────────────────────── 条目


@router.get("/entries", response_model=EntryListOut)
async def list_entries(
    memory: RuntimeDep,
    user: CurrentUserDep,
    status_: Annotated[str, Query(alias="status", pattern="^(active|deleted|all)$")] = "active",
    topic: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntryListOut:
    """按主题、来源、状态浏览（§7「查看」）。"""
    scope = memory.user_scope(user)
    rows, total = await _call(
        memory,
        user,
        lambda: memory.client.entries(
            scope,
            as_user=user,
            status=status_,
            topic=topic,
            source=source,
            q=q,
            limit=limit,
            offset=offset,
        ),
    )
    return EntryListOut(data=[_entry_out(e) for e in rows], total=total)


@router.get("/entries/{number}", response_model=EntryDetailOut)
async def get_entry(number: int, memory: RuntimeDep, user: CurrentUserDep) -> EntryDetailOut:
    """一条记忆和它的演变过程（出处、取代链）。"""
    scope = memory.user_scope(user)
    raw = await _call(memory, user, lambda: memory.client.entry(scope, number, as_user=user))
    return EntryDetailOut(
        entry=_entry_out(MemoryEntry.of(raw["entry"])),
        chain=[_entry_out(MemoryEntry.of(e)) for e in raw.get("chain") or []],
    )


async def _write(
    memory: RuntimeDep,
    user: UUID,
    scope: ScopeRef,
    *,
    topic: str,
    content: str,
    remove: list[int],
) -> WriteOut:
    result = await _call(
        memory,
        user,
        lambda: memory.client.write(
            scope,
            topic=topic,
            content=content,
            remove=remove,
            occurred_at=_now(),
            source=SOURCE_USER,
            origin=_ORIGIN,
            idempotency_key=None,
            as_user=user,
        ),
    )
    if result.get("outcome") != "applied":
        raise Conflict("这条记忆已有更新的信息，请刷新后再操作", code="discarded")
    await memory.bump_epoch(user)
    added = result.get("added") or {}
    return WriteOut(
        number=added.get("number"),
        revision=result.get("revision"),
        removed=list(result.get("removed") or []),
    )


@router.post("/entries", response_model=WriteOut, status_code=status.HTTP_201_CREATED)
async def add_entry(body: EntryCreate, memory: RuntimeDep, user: CurrentUserDep) -> WriteOut:
    """手动添加（来源记为用户亲自，补记不会改动这类记忆）。主题不存在时按给出的描述新建。"""
    scope = memory.user_scope(user)
    topics = await _call(memory, user, lambda: memory.client.catalog(scope, as_user=user))
    if body.topic not in {t.code for t in topics}:
        description = (body.topic_description or "").strip()
        if not description:
            raise AppError("主题不存在：新建主题要给出描述（收什么、不收什么）")
        await _call(
            memory,
            user,
            lambda: memory.client.create_topic(
                scope, body.topic, description, attrs={"resident": body.resident}, as_user=user
            ),
        )
    return await _write(memory, user, scope, topic=body.topic, content=body.content, remove=[])


@router.patch("/entries/{number}", response_model=WriteOut)
async def correct_entry(
    number: int, body: EntryUpdate, memory: RuntimeDep, user: CurrentUserDep
) -> WriteOut:
    """更正：删除这一条、写入新内容，在一次写入里完成（来源记为用户亲自）。"""
    scope = memory.user_scope(user)
    raw = await _call(memory, user, lambda: memory.client.entry(scope, number, as_user=user))
    entry = MemoryEntry.of(raw["entry"])
    if entry.status != "active":
        raise Conflict(f"条目 #{number} 已删除，先恢复再更正")
    topic = body.topic or entry.topic
    if not topic:
        raise AppError("找不到条目所在的主题")
    return await _write(memory, user, scope, topic=topic, content=body.content, remove=[number])


@router.post("/entries/{number}/restore", response_model=EntryOut)
async def restore_entry(
    number: int, memory: RuntimeDep, user: CurrentUserDep, body: EntryRestoreIn | None = None
) -> EntryOut:
    scope = memory.user_scope(user)
    raw = await _call(
        memory,
        user,
        lambda: memory.client.restore(
            scope,
            number,
            topic=body.topic if body else None,
            source=SOURCE_USER,
            origin=_ORIGIN,
            as_user=user,
        ),
    )
    await memory.bump_epoch(user)
    return _entry_out(MemoryEntry.of(raw["entry"]))


@router.delete("/entries/{number}", response_model=PurgeOut)
async def purge_entry(number: int, memory: RuntimeDep, user: CurrentUserDep) -> PurgeOut:
    """彻底删除：连同取代链和修订历史，不可恢复（§6.5）。"""
    scope = memory.user_scope(user)
    raw = await _call(memory, user, lambda: memory.client.purge_entry(scope, number, as_user=user))
    await memory.bump_epoch(user)
    return PurgeOut(purged=list(raw.get("purged") or []))


@router.post("/moves", status_code=status.HTTP_204_NO_CONTENT)
async def move_entries(body: MoveIn, memory: RuntimeDep, user: CurrentUserDep) -> Response:
    scope = memory.user_scope(user)
    await _call(
        memory, user, lambda: memory.client.move(scope, body.entries, body.to_topic, as_user=user)
    )
    await memory.bump_epoch(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ───────────────────────────── 修订与撤销


@router.get("/revisions", response_model=RevisionListOut)
async def list_revisions(
    memory: RuntimeDep,
    user: CurrentUserDep,
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RevisionListOut:
    scope = memory.user_scope(user)
    rows = await _call(
        memory,
        user,
        lambda: memory.client.revisions(
            scope, as_user=user, source=source, limit=limit, offset=offset
        ),
    )
    return RevisionListOut(data=[RevisionOut(**_revision_fields(r)) for r in rows])


def _revision_fields(r: dict[str, Any]) -> dict[str, Any]:
    keys = RevisionOut.model_fields
    return {k: v for k, v in r.items() if k in keys}


@router.post("/revisions/{revision_id}/undo", status_code=status.HTTP_204_NO_CONTENT)
async def undo_revision(revision_id: str, memory: RuntimeDep, user: CurrentUserDep) -> Response:
    """撤销一次写入：撤销新增 = 删除新条目；撤销取代 = 恢复旧条目、删除新条目（§4.7）。"""
    scope = memory.user_scope(user)
    await _call(
        memory,
        user,
        lambda: memory.client.undo(
            scope, revision_id, source=SOURCE_USER, origin=_ORIGIN, as_user=user
        ),
    )
    await memory.bump_epoch(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ───────────────────────────── 全部删除、暂停


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def purge_all(memory: RuntimeDep, user: CurrentUserDep) -> Response:
    """删除这位用户的全部记忆（整个用户实例，连同用户中心里的授权），不可恢复。"""
    for scope in memory.scopes_for(user):
        await _call(memory, user, lambda s=scope: memory.client.purge_instance(s, as_user=user))
    await memory.bump_epoch(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings", response_model=MemorySettingsOut)
async def get_settings_(memory: RuntimeDep, user: CurrentUserDep) -> MemorySettingsOut:
    paused, _ = await memory.user_setting(user)
    return MemorySettingsOut(paused=paused, enabled=True)


@router.put("/settings", response_model=MemorySettingsOut)
async def put_settings(
    body: MemorySettingsIn, memory: RuntimeDep, user: CurrentUserDep
) -> MemorySettingsOut:
    """暂停优于删除：暂停期间两条写入路径都停止写入，已有记忆照常常驻、照常可以加载。"""
    await memory.set_paused(user, body.paused)
    return MemorySettingsOut(paused=body.paused, enabled=True)


# ───────────────────────────── 补记提示


@router.get("/backfills", response_model=BackfillListOut)
async def list_backfills(
    session: SessionDep,
    user: CurrentUserDep,
    _: RuntimeDep,
    unseen: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BackfillListOut:
    """会话后补记写入了什么（§4.3「上次会话补记了 2 条［查看］」）。unseen=true 只列没看过的。"""
    stmt = select(MemoryBackfill).where(MemoryBackfill.user_id == user)
    if unseen:
        stmt = stmt.where(MemoryBackfill.seen_at.is_(None))
    rows = (
        await session.execute(stmt.order_by(MemoryBackfill.created_at.desc()).limit(limit))
    ).scalars()
    return BackfillListOut(
        data=[
            BackfillOut(
                id=r.id,
                thread_id=r.thread_id,
                created_at=r.created_at.isoformat(),
                seen_at=r.seen_at.isoformat() if r.seen_at else None,
                written=list(r.written or []),
                split=list(r.split or []),
            )
            for r in rows
        ]
    )


@router.post("/backfills/seen")
async def mark_backfills_seen(
    body: BackfillSeenIn, session: SessionDep, user: CurrentUserDep, _: RuntimeDep
) -> dict[str, int]:
    stmt = update(MemoryBackfill).where(
        MemoryBackfill.user_id == user, MemoryBackfill.seen_at.is_(None)
    )
    if body.ids is not None:
        stmt = stmt.where(MemoryBackfill.id.in_(body.ids))
    result = await session.execute(stmt.values(seen_at=_now()))
    await session.commit()
    return {"updated": int(result.rowcount or 0)}  # type: ignore[attr-defined]
