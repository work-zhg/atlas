"""作用域实例的接口（§3.3–§3.7）。

  /v1/scopes/{scope}/instances/{key}/…      key 写成 字段=值，多个字段用逗号分隔

登记归属      POST   …/register                           不校验（第一次登记的 Owner 生效）
主题目录      GET    …/topics                             只读
新建主题      POST   …/topics                             读写
加载主题      GET    …/topics/{code}                      只读：主题 + 全部有效条目
修改主题      PATCH  …/topics/{code}                      读写：描述、扩展属性
删除空主题    DELETE …/topics/{code}                      读写
拆分主题      POST   …/topics/{code}/split                读写
写入          POST   …/writes                             读写：删除若干 + 至多新增一条
移动条目      POST   …/moves                              读写
浏览条目      GET    …/entries                            只读
条目详情      GET    …/entries/{number}                   只读：含取代链
恢复条目      POST   …/entries/{number}/restore           读写
彻底删除条目  DELETE …/entries/{number}                   Owner：连同取代链与修订历史
修订历史      GET    …/revisions                          只读
撤销写入      POST   …/revisions/{id}/undo                读写
彻底删除实例  DELETE …                                    Owner：连同用户中心里的授权

列出实例      GET    /v1/scopes/{scope}/instances?字段=值  按部分关联主键过滤
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from ..auth import caller_of
from ..errors import Invalid, Unauthorized
from ..keys import parse_key, validate_key
from ..perms import LEVEL_RANK
from ..store import SplitGroup, WriteRequest, entry_view, revision_view, topic_view
from .deps import OwnerAccess, PermsDep, ReadAccess, StoreDep, WriteAccess
from .schemas import (
    MoveIn,
    RegisterIn,
    RestoreIn,
    SplitIn,
    TopicCreate,
    TopicPatch,
    UndoIn,
    WriteIn,
)

router = APIRouter(prefix="/v1/scopes/{scope}", tags=["instances"])

_BASE = "/instances/{key}"


def _instance_view(acc: Any) -> dict[str, Any]:
    inst = acc.instance
    return {
        "scope": acc.scope.code,
        "key": acc.key,
        "id": str(inst.uuid) if inst else None,
        "version": inst.version_seq if inst else 0,
    }


# ── 实例


@router.get("/instances")
async def list_instances(
    scope: str, request: Request, store: StoreDep, perms: PermsDep, limit: int = Query(200, le=500)
) -> dict[str, Any]:
    """按部分关联主键列实例（例如删除某个用户的全部数据前，列出 uid=… 的全部实例）。

    打开数据权限开关的作用域只列出调用用户至少能读的。
    """
    sc = await store.scope(scope)
    partial = {k: v for k, v in request.query_params.items() if k in sc.key_fields}
    if partial:
        validate_key(list(partial), partial)
    rows = await store.list_instances(sc, partial, limit=limit)
    if sc.check_permission:
        caller = caller_of(request)
        if caller is None:
            raise Unauthorized("该作用域校验数据权限，请求要带调用用户（X-Uid）")
        assert sc.data_code
        ids, type_level = await perms.accessible(caller.auth_uid, sc.data_code, "READ")
        if LEVEL_RANK.get(type_level, 0) < LEVEL_RANK["READ"]:
            rows = [r for r in rows if str(r.uuid) in ids]
    return {
        "data": [
            {
                "key": r.key,
                "id": str(r.uuid),
                "version": r.version_seq,
                "registered": r.uc_registered,
            }
            for r in rows
        ]
    }


@router.post(_BASE + "/register")
async def register(
    scope: str, key: str, body: RegisterIn, store: StoreDep, perms: PermsDep
) -> dict[str, Any]:
    """登记实例归属（§3.7）。幂等：已登记的直接返回；开关关闭的作用域只建实例，不碰用户中心。"""
    sc = await store.scope(scope)
    parsed = parse_key(sc.key_fields, key)
    inst, written = await store.register(sc, parsed, body.owner_uid, perms)
    return {
        "scope": sc.code,
        "key": parsed,
        "id": str(inst.uuid),
        "check_permission": sc.check_permission,
        "registered": inst.uc_registered,
        "owner_written": written,
    }


@router.delete(_BASE, status_code=204)
async def purge_instance(acc: OwnerAccess, perms: PermsDep) -> Response:
    """彻底删除整个实例。打开开关的作用域先清用户中心里的授权（失败则整体不删，稍后重试）。"""
    inst = await acc.locked_existing()
    if inst is not None:
        if acc.scope.check_permission and inst.uc_registered:
            assert acc.scope.data_code
            await perms.cleanup(acc.scope.data_code, str(inst.uuid))
        await acc.store.purge_instance(inst)
    return Response(status_code=204)


# ── 主题


@router.get(_BASE + "/topics")
async def catalog(acc: ReadAccess) -> dict[str, Any]:
    """主题目录：每个主题的编码、描述、扩展属性、当前大小、版本号。"""
    topics = await acc.store.catalog(acc.instance)
    return {**_instance_view(acc), "topics": [topic_view(t) for t in topics]}


@router.post(_BASE + "/topics", status_code=201)
async def create_topic(acc: WriteAccess, body: TopicCreate) -> dict[str, Any]:
    inst = await acc.locked()
    topic = await acc.store.create_topic(inst, body.code, body.description, body.attrs)
    return topic_view(topic)


@router.get(_BASE + "/topics/{code}")
async def load_topic(acc: ReadAccess, code: str) -> dict[str, Any]:
    """一个主题和它的全部有效条目（按编号排）。"""
    topic, entries = await acc.store.load_topic(acc.instance, code)
    return {"topic": topic_view(topic), "entries": [entry_view(e, topic.code) for e in entries]}


@router.patch(_BASE + "/topics/{code}")
async def update_topic(acc: WriteAccess, code: str, body: TopicPatch) -> dict[str, Any]:
    inst = await acc.locked()
    topic, change = await acc.store.update_topic(
        inst, code, description=body.description, attrs=body.attrs
    )
    return {"topic": topic_view(topic), "changed": [change.as_dict()]}


@router.delete(_BASE + "/topics/{code}", status_code=204)
async def delete_topic(acc: WriteAccess, code: str) -> Response:
    inst = await acc.locked()
    await acc.store.delete_topic(inst, code)
    return Response(status_code=204)


@router.post(_BASE + "/topics/{code}/split")
async def split_topic(acc: WriteAccess, code: str, body: SplitIn) -> dict[str, Any]:
    inst = await acc.locked()
    created, changes = await acc.store.split(
        inst,
        code,
        body.expected_version,
        [SplitGroup(g.code, g.description, g.entries, g.attrs) for g in body.groups],
    )
    return {"topics": [topic_view(t) for t in created], "changed": [c.as_dict() for c in changes]}


# ── 写入与移动


@router.post(_BASE + "/writes")
async def write(acc: WriteAccess, body: WriteIn) -> dict[str, Any]:
    inst = await acc.locked()
    return await acc.store.write(
        inst,
        WriteRequest(
            topic=body.topic,
            content=body.content,
            remove=list(body.remove),
            occurred_at=body.occurred_at,
            source=body.source,
            origin=body.origin,
            idempotency_key=body.idempotency_key,
        ),
    )


@router.post(_BASE + "/moves")
async def move(acc: WriteAccess, body: MoveIn) -> dict[str, Any]:
    inst = await acc.locked()
    changes = await acc.store.move(inst, list(body.entries), body.to_topic)
    return {"changed": [c.as_dict() for c in changes]}


# ── 条目


_ORIGIN_QUERY = Query(None, description='出处过滤：JSON 对象，按包含关系匹配，如 {"thread": "…"}')


@router.get(_BASE + "/entries")
async def list_entries(
    acc: ReadAccess,
    status: str = Query("active", pattern="^(active|deleted|all)$"),
    topic: str | None = None,
    source: str | None = None,
    origin: str | None = _ORIGIN_QUERY,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows, total = await acc.store.list_entries(
        acc.instance,
        status=status,
        topic=topic,
        source=source,
        origin=_origin_filter(origin),
        q=q,
        limit=limit,
        offset=offset,
    )
    return {"data": [entry_view(e, c) for e, c in rows], "total": total}


@router.get(_BASE + "/entries/{number}")
async def entry_detail(acc: ReadAccess, number: int) -> dict[str, Any]:
    return await acc.store.entry_detail(acc.instance, number)


@router.post(_BASE + "/entries/{number}/restore")
async def restore_entry(
    acc: WriteAccess, number: int, body: RestoreIn | None = None
) -> dict[str, Any]:
    body = body or RestoreIn()
    inst = await acc.locked()
    entry, home, changes = await acc.store.restore(
        inst, number, topic=body.topic, source=body.source, origin=body.origin
    )
    return {"entry": entry_view(entry, home.code), "changed": [c.as_dict() for c in changes]}


@router.delete(_BASE + "/entries/{number}")
async def purge_entry(acc: OwnerAccess, number: int) -> dict[str, Any]:
    inst = await acc.locked()
    gone, changes = await acc.store.purge_entry(inst, number)
    return {"purged": gone, "changed": [c.as_dict() for c in changes]}


# ── 修订


@router.get(_BASE + "/revisions")
async def list_revisions(
    acc: ReadAccess,
    kind: str | None = None,
    source: str | None = None,
    origin: str | None = _ORIGIN_QUERY,
    since: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = await acc.store.list_revisions(
        acc.instance,
        kind=kind,
        source=source,
        origin=_origin_filter(origin),
        since=since,
        limit=limit,
        offset=offset,
    )
    return {"data": [revision_view(r) for r in rows]}


@router.post(_BASE + "/revisions/{revision_id}/undo")
async def undo(acc: WriteAccess, revision_id: UUID, body: UndoIn | None = None) -> dict[str, Any]:
    body = body or UndoIn()
    inst = await acc.locked()
    rev, changes = await acc.store.undo(inst, revision_id, source=body.source, origin=body.origin)
    return {"revision": revision_view(rev), "changed": [c.as_dict() for c in changes]}


def _origin_filter(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise Invalid("origin 要是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise Invalid("origin 要是 JSON 对象")
    return value or None
