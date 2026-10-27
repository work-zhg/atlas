"""记忆服务的规则（doc/memory-design.html §3.3–§3.6）：主题、条目、写入、拆分、撤销、彻底删除。

## 实例锁

同一个作用域实例的写入（含拆分的提交）一次只执行一个：先锁实例行（SELECT … FOR UPDATE），
锁随请求的事务提交释放。★ 用数据库行锁而不是 Redis 锁：锁和事务同生共死，多副本部署同样有效，
没有「锁过期了事务还在写」的窗口；条目编号、版本号也就能直接在实例行上递增。
读不加锁。

## 一次写入（§3.5）

一次写入 = 删除若干旧条目 + 至多新增一条，在一个事务里完成。每次写入都带客户端给的时间，
以时间晚的为准。要删除的每一条先找到它「现在的样子」：

    条目还有效                    → 就是它
    已被取代（删除且有后继）      → 沿取代关系找到现在的有效条目
    已被遗忘（删除且没有后继）    → 那次遗忘

    现在的有效条目时间晚于本次写入   → 本次写入作废（已有更新的信息）
    遗忘的时间晚于本次写入           → 本次写入作废
    否则                              → 删除那条有效条目（遗忘的就只做新增）

时间相同按「后到的为准」继续。两次写入如果互相没看到对方、又都只做新增，两条会同时存在 ——
服务端不判断语义，留给下一次写入这个主题的客户端清理（§6.2）。

## 版本号（§3.3）

主题里的条目有任何变化（新增、删除、移动、撤销、恢复、拆分），或主题的描述、扩展属性变了，
版本号就变。一次操作从实例计数器取一个新值，写给所有受影响的主题；写入返回每个受影响主题
写入前、写入后的版本号，客户端据此判断本地内容是否最新（§5.5）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Entry, Instance, Revision, Scope, Topic
from .db.types import utcnow, uuid7
from .errors import Conflict, Invalid, NotFound, TopicGone, VersionMismatch
from .keys import canonical
from .perms import DataPermissions

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryStore",
    "SplitGroup",
    "TopicChange",
    "WriteRequest",
    "entry_view",
    "topic_view",
]

_ATTRS_MAX_BYTES = 4_096
_ORIGIN_MAX_BYTES = 2_048
_CONTROL = re.compile(r"[\x00-\x1f]")


# ───────────────────────────── 输入与输出


@dataclass(frozen=True)
class WriteRequest:
    #: 新增条目的目标主题（只删除时可以不给）
    topic: str | None
    #: 新增的内容；None = 只删除（遗忘）
    content: str | None
    remove: list[int]
    occurred_at: datetime
    source: str = ""
    origin: dict[str, Any] | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class SplitGroup:
    code: str
    description: str
    entries: list[int]
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TopicChange:
    code: str
    version_before: int
    version_after: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.code,
            "version_before": self.version_before,
            "version_after": self.version_after,
        }


def topic_view(t: Topic) -> dict[str, Any]:
    return {
        "code": t.code,
        "description": t.description,
        "attrs": dict(t.attrs or {}),
        "status": t.status,
        "version": t.version,
        "entry_count": t.entry_count,
        "char_count": t.char_count,
        "split_into": list(t.split_into) if t.split_into else None,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


def entry_view(e: Entry, topic_code: str | None) -> dict[str, Any]:
    return {
        "number": e.number,
        "content": e.content,
        "topic": topic_code,
        "occurred_at": e.occurred_at.isoformat(),
        "source": e.source,
        "origin": e.origin,
        "status": e.status,
        "superseded_by": e.superseded_by,
        "retired_at": e.retired_at.isoformat() if e.retired_at else None,
        "created_at": e.created_at.isoformat(),
    }


def revision_view(r: Revision) -> dict[str, Any]:
    return {
        "id": str(r.uuid),
        "kind": r.kind,
        "topic": r.topic_code,
        "removed": list(r.removed or []),
        "added": list(r.added or []),
        "occurred_at": r.occurred_at.isoformat(),
        "source": r.source,
        "origin": r.origin,
        "detail": r.detail,
        "undoes": str(r.undoes_uuid) if r.undoes_uuid else None,
        "undone_by": str(r.undone_by_uuid) if r.undone_by_uuid else None,
        "created_at": r.created_at.isoformat(),
    }


def _json_contains(column: Any, value: Mapping[str, Any]) -> Any:
    """JSONB 包含（@>）。列类型是 JSON/JSONB 变体，通用 JSON 比较器没有 contains。

    ★ 用 type_coerce 而不是 cast(json.dumps(…), JSONB)：后者会让 JSONB 的绑定处理器把那段
      字符串再序列化一次，变成一个 JSON 字符串，包含判断永远为假。
    """
    return column.op("@>")(sa.type_coerce(dict(value), postgresql.JSONB))


def check_topic_code(code: str) -> str:
    if not code or len(code) > 64 or code != code.strip() or "/" in code or _CONTROL.search(code):
        raise Invalid("主题编码不合法：1–64 个字符，首尾不能是空白，不能含 / 和控制字符")
    return code


def _check_json(value: Mapping[str, Any] | None, what: str, limit: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if len(json.dumps(dict(value), ensure_ascii=False).encode()) > limit:
        raise Invalid(f"{what}过大（上限 {limit} 字节）")
    return dict(value)


def _aware(ts: datetime, what: str) -> datetime:
    if ts.tzinfo is None:
        raise Invalid(f"{what}必须带时区")
    return ts


# ───────────────────────────── 存储


class MemoryStore:
    def __init__(
        self, session: AsyncSession, *, entry_max_chars: int = 4_000, max_removes: int = 50
    ) -> None:
        self.session = session
        self._entry_max = entry_max_chars
        self._max_removes = max_removes

    # ── 作用域与实例

    async def scope(self, code: str) -> Scope:
        row = await self.session.scalar(select(Scope).where(Scope.code == code))
        if row is None:
            raise NotFound(f"作用域 {code} 不存在", code="SCOPE_NOT_FOUND")
        return row

    async def find_instance(self, scope: Scope, key: Mapping[str, str]) -> Instance | None:
        return await self.session.scalar(
            select(Instance).where(
                Instance.scope_uuid == scope.uuid,
                Instance.key_text == canonical(scope.key_fields, key),
            )
        )

    async def lock_instance(
        self, scope: Scope, key: Mapping[str, str], *, create: bool
    ) -> Instance | None:
        """锁住实例行（不存在且 create 时先建）。

        ★ populate_existing：实例行可能在加锁前就被读进了会话（权限检查），那时的计数器
          可能已被别的事务推进 —— 必须以加锁后读到的为准。
        """
        key_text = canonical(scope.key_fields, key)
        if create:
            now = utcnow()
            await self.session.execute(
                pg_insert(Instance)
                .values(
                    uuid=uuid7(),
                    scope_uuid=scope.uuid,
                    key=dict(key),
                    key_text=key_text,
                    version_seq=0,
                    entry_seq=0,
                    uc_registered=False,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["scope_uuid", "key_text"])
            )
        return await self.session.scalar(
            select(Instance)
            .where(Instance.scope_uuid == scope.uuid, Instance.key_text == key_text)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def register(
        self, scope: Scope, key: Mapping[str, str], owner_uid: str, perms: DataPermissions
    ) -> tuple[Instance, bool]:
        """登记实例归属（§3.7）。返回 (实例, 这次是否写入了 Owner)。

        开关关闭的作用域只建实例行，不碰用户中心。已登记的直接返回。
        """
        inst = await self.lock_instance(scope, key, create=True)
        assert inst is not None
        if not scope.check_permission or inst.uc_registered:
            return inst, False
        assert scope.data_code
        name = f"{scope.description or scope.code}：{inst.key_text}"
        written = await perms.register(scope.data_code, str(inst.uuid), name, owner_uid)
        inst.uc_registered = True
        if written:
            inst.owner_uid = owner_uid
        return inst, written

    async def list_instances(
        self, scope: Scope, partial: Mapping[str, str], *, limit: int = 200
    ) -> list[Instance]:
        stmt = select(Instance).where(Instance.scope_uuid == scope.uuid)
        if partial:
            stmt = stmt.where(_json_contains(Instance.key, partial))
        return list((await self.session.scalars(stmt.order_by(Instance.id).limit(limit))).all())

    async def purge_instance(self, inst: Instance) -> dict[str, int]:
        """彻底删除整个实例：主题、条目、修订历史、实例行。用户中心的授权由调用方清理。"""
        counts = {}
        for model, name in ((Entry, "entries"), (Revision, "revisions"), (Topic, "topics")):
            result = await self.session.execute(
                sa.delete(model).where(model.instance_uuid == inst.uuid)
            )
            counts[name] = int(result.rowcount or 0)  # type: ignore[attr-defined]
        await self.session.delete(inst)
        await self.session.flush()
        return counts

    # ── 主题

    async def catalog(self, inst: Instance | None) -> list[Topic]:
        if inst is None:
            return []
        return list(
            (
                await self.session.scalars(
                    select(Topic)
                    .where(Topic.instance_uuid == inst.uuid, Topic.status == "active")
                    .order_by(Topic.id)
                )
            ).all()
        )

    async def _active_topic(self, inst: Instance, code: str) -> Topic | None:
        return await self.session.scalar(
            select(Topic).where(
                Topic.instance_uuid == inst.uuid, Topic.code == code, Topic.status == "active"
            )
        )

    async def _latest_topic(self, inst: Instance, code: str) -> Topic | None:
        return await self.session.scalar(
            select(Topic)
            .where(Topic.instance_uuid == inst.uuid, Topic.code == code)
            .order_by(Topic.id.desc())
            .limit(1)
        )

    async def topic(self, inst: Instance | None, code: str) -> Topic:
        """有效主题；不存在 / 已拆分时报错并带上当前主题目录（§3.6）。"""
        if inst is not None:
            found = await self._active_topic(inst, code)
            if found is not None:
                return found
            gone = await self._latest_topic(inst, code)
            if gone is not None:
                raise TopicGone(
                    f"主题 {code} 已被{'拆分' if gone.status == 'split' else '删除'}",
                    topic=code,
                    split_into=list(gone.split_into or []),
                    catalog=[topic_view(t) for t in await self.catalog(inst)],
                )
        raise NotFound(
            f"主题 {code} 不存在",
            code="TOPIC_NOT_FOUND",
            topic=code,
            catalog=[topic_view(t) for t in await self.catalog(inst)],
        )

    async def create_topic(
        self, inst: Instance, code: str, description: str, attrs: Mapping[str, Any] | None
    ) -> Topic:
        check_topic_code(code)
        if (existing := await self._active_topic(inst, code)) is not None:
            raise Conflict(f"主题 {code} 已存在", code="TOPIC_EXISTS", topic=topic_view(existing))
        topic = Topic(
            instance_uuid=inst.uuid,
            code=code,
            description=description.strip(),
            attrs=_check_json(attrs or {}, "扩展属性", _ATTRS_MAX_BYTES) or {},
            status="active",
            version=self._next_version(inst),
        )
        self.session.add(topic)
        await self.session.flush()
        return topic

    async def update_topic(
        self,
        inst: Instance,
        code: str,
        *,
        description: str | None,
        attrs: Mapping[str, Any] | None,
    ) -> tuple[Topic, TopicChange]:
        topic = await self.topic(inst, code)
        if description is not None:
            topic.description = description.strip()
        if attrs is not None:
            topic.attrs = _check_json(attrs, "扩展属性", _ATTRS_MAX_BYTES) or {}
        # ★ 描述、扩展属性变了也换版本号：客户端的常驻快照里有它们
        [change] = await self._touch(inst, [topic], recount=False)
        return topic, change

    async def delete_topic(self, inst: Instance, code: str) -> None:
        """删除空主题。有有效条目的不能删 —— 先移走或遗忘（删除条目要经修订历史，不能连带）。"""
        topic = await self.topic(inst, code)
        if topic.entry_count:
            raise Conflict(f"主题 {code} 里还有 {topic.entry_count} 条有效条目，不能删除")
        topic.status = "deleted"
        await self._touch(inst, [topic], recount=False)

    async def load_topic(self, inst: Instance | None, code: str) -> tuple[Topic, list[Entry]]:
        topic = await self.topic(inst, code)
        entries = (
            await self.session.scalars(
                select(Entry)
                .where(Entry.topic_uuid == topic.uuid, Entry.status == "active")
                .order_by(Entry.number)
            )
        ).all()
        return topic, list(entries)

    # ── 写入（§3.5）

    async def write(self, inst: Instance, req: WriteRequest) -> dict[str, Any]:
        """一次写入。返回结果（也原样存在修订上，供幂等重放）：

        outcome    applied | discarded
        revision   这次修订的 id（discarded / 什么都没变时为 None）
        added      新增的条目
        removed    实际删除的条目编号（沿取代关系找到的那些）
        skipped    已被更早地遗忘、因而只做新增的编号
        changed    受影响主题写入前后的版本号
        conflicts  作废时：哪些条目已有更新的信息
        """
        if req.idempotency_key:
            replay = await self._replay(inst, req.idempotency_key)
            if replay is not None:
                return replay
        _aware(req.occurred_at, "写入时间")
        if req.content is None and not req.remove:
            raise Invalid("一次写入至少要删除一条或新增一条")
        if len(req.remove) > self._max_removes:
            raise Invalid(f"一次写入最多删除 {self._max_removes} 条")
        content = None
        if req.content is not None:
            content = req.content.strip()
            if not content:
                raise Invalid("新增的内容不能为空")
            if len(content) > self._entry_max:
                raise Invalid(f"单条内容最多 {self._entry_max} 个字符")
            if not req.topic:
                raise Invalid("新增条目要给出目标主题")
        origin = _check_json(req.origin, "出处", _ORIGIN_MAX_BYTES)

        target = await self.topic(inst, req.topic) if content is not None and req.topic else None

        heads: dict[int, Entry] = {}
        skipped: list[int] = []
        conflicts: list[dict[str, Any]] = []
        for number in dict.fromkeys(req.remove):
            entry = await self._entry(inst, number)
            head, forgotten_at = await self._current(inst, entry)
            if head is not None:
                if head.occurred_at > req.occurred_at:
                    conflicts.append(
                        {
                            "number": number,
                            "current": head.number,
                            "current_at": head.occurred_at.isoformat(),
                        }
                    )
                else:
                    heads[head.number] = head
            elif forgotten_at is not None and forgotten_at > req.occurred_at:
                conflicts.append(
                    {"number": number, "current": None, "current_at": forgotten_at.isoformat()}
                )
            else:
                skipped.append(number)
        if conflicts:
            return {
                "outcome": "discarded",
                "reason": "newer",
                "revision": None,
                "added": None,
                "removed": [],
                "skipped": [],
                "changed": [],
                "conflicts": conflicts,
            }
        if content is None and not heads:
            # 要遗忘的都已被更早地遗忘：什么都不用做
            return {
                "outcome": "applied",
                "revision": None,
                "added": None,
                "removed": [],
                "skipped": skipped,
                "changed": [],
                "conflicts": [],
            }

        rev = Revision(
            uuid=uuid7(),
            instance_uuid=inst.uuid,
            kind="write",
            topic_code=target.code if target is not None else None,
            occurred_at=req.occurred_at,
            source=req.source or None,
            origin=origin,
            idempotency_key=req.idempotency_key,
        )
        new_entry: Entry | None = None
        if content is not None and target is not None:
            inst.entry_seq += 1
            new_entry = Entry(
                instance_uuid=inst.uuid,
                topic_uuid=target.uuid,
                number=inst.entry_seq,
                content=content,
                occurred_at=req.occurred_at,
                source=req.source,
                origin=origin,
                status="active",
                created_rev_uuid=rev.uuid,
            )
            self.session.add(new_entry)
        for head in heads.values():
            head.status = "deleted"
            head.retired_rev_uuid = rev.uuid
            head.retired_at = req.occurred_at
            head.superseded_by = new_entry.number if new_entry is not None else None

        touched = await self._topics_of(inst, [h.topic_uuid for h in heads.values()])
        if target is not None:
            touched.append(target)
        changes = await self._touch(inst, touched)

        rev.removed = sorted(heads)
        rev.added = [new_entry.number] if new_entry is not None else []
        self.session.add(rev)
        await self.session.flush()
        result = {
            "outcome": "applied",
            "revision": str(rev.uuid),
            "added": entry_view(new_entry, target.code) if new_entry and target else None,
            "removed": sorted(heads),
            "skipped": skipped,
            "changed": [c.as_dict() for c in changes],
            "conflicts": [],
        }
        rev.result = result
        return result

    async def _replay(self, inst: Instance, key: str) -> dict[str, Any] | None:
        rev = await self.session.scalar(
            select(Revision).where(
                Revision.instance_uuid == inst.uuid, Revision.idempotency_key == key
            )
        )
        if rev is None:
            return None
        if rev.result is None:
            raise Conflict("这个幂等键对应的写入已被彻底删除", code="IDEMPOTENCY_GONE")
        return {**rev.result, "replayed": True}

    async def _entry(self, inst: Instance, number: int) -> Entry:
        row = await self.session.scalar(
            select(Entry).where(Entry.instance_uuid == inst.uuid, Entry.number == number)
        )
        if row is None:
            raise NotFound(f"条目 #{number} 不存在", code="ENTRY_NOT_FOUND", number=number)
        return row

    async def _current(self, inst: Instance, entry: Entry) -> tuple[Entry | None, datetime | None]:
        """条目现在的样子：(有效条目, None) 或 (None, 遗忘的时间)。见模块说明。"""
        cur = entry
        seen: set[int] = set()
        while cur.status != "active":
            if cur.superseded_by is None or cur.number in seen:
                return None, cur.retired_at
            seen.add(cur.number)
            nxt = await self.session.scalar(
                select(Entry).where(
                    Entry.instance_uuid == inst.uuid, Entry.number == cur.superseded_by
                )
            )
            if nxt is None:
                return None, cur.retired_at
            cur = nxt
        return cur, None

    # ── 移动（§3.3）

    async def move(self, inst: Instance, numbers: list[int], to_code: str) -> list[TopicChange]:
        """把条目移到另一个主题。不改条目的内容、编号、时间和来源。"""
        if not numbers:
            raise Invalid("至少给出一条条目")
        target = await self.topic(inst, to_code)
        entries = [await self._entry(inst, n) for n in dict.fromkeys(numbers)]
        for e in entries:
            if e.status != "active":
                raise Conflict(f"条目 #{e.number} 已删除，不能移动", code="ENTRY_NOT_ACTIVE")
        sources = await self._topics_of(inst, [e.topic_uuid for e in entries])
        for e in entries:
            e.topic_uuid = target.uuid
        changes = await self._touch(inst, [*sources, target])
        self.session.add(
            Revision(
                instance_uuid=inst.uuid,
                kind="move",
                topic_code=target.code,
                occurred_at=utcnow(),
                detail={
                    "entries": [e.number for e in entries],
                    "from": sorted({t.code for t in sources if t.uuid != target.uuid}),
                    "to": target.code,
                },
            )
        )
        await self.session.flush()
        return changes

    # ── 拆分（§3.6）

    async def split(
        self, inst: Instance, code: str, expected_version: int, groups: list[SplitGroup]
    ) -> tuple[list[Topic], list[TopicChange]]:
        """按客户端给的分组拆分主题：新建子主题、移动条目、原主题标为已拆分，一个事务完成。

        ★ 只移动、不改写；主题版本号和客户端分组时看到的不一致（分组期间有新的写入），
          或者分组没有正好覆盖原主题的全部有效条目，一律拒绝 —— 客户端下次再拆。
        """
        topic = await self.topic(inst, code)
        if topic.version != expected_version:
            raise VersionMismatch(
                f"主题 {code} 在分组期间有变化（版本 {expected_version} → {topic.version}）",
                topic=code,
                version=topic.version,
            )
        if len(groups) < 2:
            raise Invalid("拆分至少要分成两组")
        codes = [check_topic_code(g.code) for g in groups]
        if len(codes) != len(set(codes)):
            raise Invalid("分组的主题编码不能重复")
        for c in codes:
            if c != code and await self._active_topic(inst, c) is not None:
                raise Conflict(f"主题 {c} 已存在", code="TOPIC_EXISTS")
        active = {
            n
            for (n,) in (
                await self.session.execute(
                    select(Entry.number).where(
                        Entry.topic_uuid == topic.uuid, Entry.status == "active"
                    )
                )
            ).all()
        }
        listed = [n for g in groups for n in g.entries]
        if any(not g.entries for g in groups):
            raise Invalid("每一组至少要有一条条目")
        if len(listed) != len(set(listed)) or set(listed) != active:
            raise Invalid(
                "分组没有正好覆盖原主题的全部有效条目",
                code="SPLIT_COVERAGE",
                missing=sorted(active - set(listed)),
                unknown=sorted(set(listed) - active),
            )

        version_before = topic.version
        version = self._next_version(inst)
        topic.status = "split"
        topic.split_into = codes
        topic.version = version
        topic.entry_count = topic.char_count = 0
        # ★ 先让原主题让出编码（部分唯一索引只管有效主题），子主题才能沿用同一个编码
        await self.session.flush()

        created: list[Topic] = []
        for g in groups:
            child = Topic(
                instance_uuid=inst.uuid,
                code=g.code,
                description=g.description.strip(),
                attrs=_check_json(g.attrs, "扩展属性", _ATTRS_MAX_BYTES) or {},
                status="active",
                version=version,
            )
            self.session.add(child)
            created.append(child)
        await self.session.flush()
        by_number = {
            e.number: e
            for e in (
                await self.session.scalars(
                    select(Entry).where(Entry.topic_uuid == topic.uuid, Entry.status == "active")
                )
            ).all()
        }
        for g, child in zip(groups, created, strict=True):
            for n in g.entries:
                by_number[n].topic_uuid = child.uuid
        await self._recount(created)
        self.session.add(
            Revision(
                instance_uuid=inst.uuid,
                kind="split",
                topic_code=code,
                occurred_at=utcnow(),
                detail={
                    "from": code,
                    "groups": [{"code": g.code, "entries": g.entries} for g in groups],
                },
            )
        )
        await self.session.flush()
        changes = [TopicChange(code, version_before, version)]
        changes += [TopicChange(c.code, 0, version) for c in created]
        return created, changes

    # ── 浏览

    async def list_entries(
        self,
        inst: Instance | None,
        *,
        status: str = "active",
        topic: str | None = None,
        source: str | None = None,
        origin: Mapping[str, Any] | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[tuple[Entry, str]], int]:
        if inst is None:
            return [], 0
        stmt = (
            select(Entry, Topic.code)
            .join(Topic, Topic.uuid == Entry.topic_uuid)
            .where(Entry.instance_uuid == inst.uuid)
        )
        if status != "all":
            stmt = stmt.where(Entry.status == status)
        if topic:
            stmt = stmt.where(Topic.code == topic)
        if source:
            stmt = stmt.where(Entry.source == source)
        if origin:
            stmt = stmt.where(_json_contains(Entry.origin, origin))
        if q:
            stmt = stmt.where(Entry.content.ilike(f"%{q}%"))
        total = int(
            await self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )
        rows = (
            await self.session.execute(
                stmt.order_by(Entry.number.desc()).offset(offset).limit(limit)
            )
        ).all()
        return [(e, c) for e, c in rows], total

    async def entry_detail(self, inst: Instance | None, number: int) -> dict[str, Any]:
        """一条条目和它的演变过程（取代链上的全部条目，按编号排）。"""
        if inst is None:
            raise NotFound(f"条目 #{number} 不存在", code="ENTRY_NOT_FOUND")
        entry = await self._entry(inst, number)
        chain = await self._component(inst, entry)
        codes = await self._codes([e.topic_uuid for e in chain])
        return {
            "entry": entry_view(entry, codes.get(entry.topic_uuid)),
            "chain": [entry_view(e, codes.get(e.topic_uuid)) for e in sorted(chain, key=_number)],
        }

    async def list_revisions(
        self,
        inst: Instance | None,
        *,
        kind: str | None = None,
        source: str | None = None,
        origin: Mapping[str, Any] | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Revision]:
        if inst is None:
            return []
        stmt = select(Revision).where(Revision.instance_uuid == inst.uuid)
        if kind:
            stmt = stmt.where(Revision.kind == kind)
        if source:
            stmt = stmt.where(Revision.source == source)
        if origin:
            stmt = stmt.where(_json_contains(Revision.origin, origin))
        if since is not None:
            stmt = stmt.where(Revision.created_at >= _aware(since, "since"))
        rows = await self.session.scalars(
            stmt.order_by(Revision.id.desc()).offset(offset).limit(limit)
        )
        return list(rows.all())

    # ── 撤销、恢复（§4.7、§7）

    async def undo(
        self,
        inst: Instance,
        revision_id: UUID,
        *,
        source: str = "",
        origin: Mapping[str, Any] | None = None,
    ) -> tuple[Revision, list[TopicChange]]:
        """把一次写入反向执行：删除新条目、恢复被它删除的旧条目。

        ★ 只在这次写入的结果仍是现状时允许：新条目还有效、旧条目还是被它删除的状态。
          之后又有改动（新条目被再次取代、旧条目被恢复过……）就拒绝，让用户看清现状再操作 ——
          硬撤会把后来的改动一并抹掉。
        """
        rev = await self.session.scalar(
            select(Revision).where(
                Revision.instance_uuid == inst.uuid, Revision.uuid == revision_id
            )
        )
        if rev is None:
            raise NotFound("修订不存在", code="REVISION_NOT_FOUND")
        if rev.kind != "write":
            raise Invalid("只能撤销写入")
        if rev.undone_by_uuid is not None:
            raise Conflict("这次写入已经撤销过了", code="ALREADY_UNDONE")
        added = [await self._entry(inst, n) for n in rev.added]
        removed = [await self._entry(inst, n) for n in rev.removed]
        stale = [e.number for e in added if e.status != "active" or e.created_rev_uuid != rev.uuid]
        stale += [
            e.number for e in removed if e.status != "deleted" or e.retired_rev_uuid != rev.uuid
        ]
        if stale:
            raise Conflict(
                "这次写入之后又有新的改动，不能直接撤销", code="UNDO_STALE", entries=stale
            )

        now = utcnow()
        undo = Revision(
            uuid=uuid7(),
            instance_uuid=inst.uuid,
            kind="undo",
            topic_code=rev.topic_code,
            removed=[e.number for e in added],
            added=[e.number for e in removed],
            occurred_at=now,
            source=source or None,
            origin=_check_json(origin, "出处", _ORIGIN_MAX_BYTES),
            undoes_uuid=rev.uuid,
        )
        hint = added[0].topic_uuid if added else None
        touched: list[Topic] = await self._topics_of(inst, [e.topic_uuid for e in added])
        for e in added:
            e.status = "deleted"
            e.retired_rev_uuid = undo.uuid
            e.retired_at = now
            e.superseded_by = None
        for e in removed:
            home = await self._home_topic(inst, e, hint)
            e.topic_uuid = home.uuid
            e.status = "active"
            e.retired_rev_uuid = None
            e.retired_at = None
            e.superseded_by = None
            touched.append(home)
        rev.undone_by_uuid = undo.uuid
        self.session.add(undo)
        changes = await self._touch(inst, touched)
        await self.session.flush()
        return undo, changes

    async def restore(
        self,
        inst: Instance,
        number: int,
        *,
        topic: str | None = None,
        source: str = "",
        origin: Mapping[str, Any] | None = None,
    ) -> tuple[Entry, Topic, list[TopicChange]]:
        """把已删除的条目恢复为有效。原主题已拆分 / 删除时放进拆出来的主题，或调用方指定的主题。"""
        entry = await self._entry(inst, number)
        if entry.status == "active":
            raise Conflict(f"条目 #{number} 已是有效状态", code="ENTRY_ACTIVE")
        home = await self.topic(inst, topic) if topic else await self._home_topic(inst, entry, None)
        entry.topic_uuid = home.uuid
        entry.status = "active"
        entry.retired_rev_uuid = None
        entry.retired_at = None
        entry.superseded_by = None
        self.session.add(
            Revision(
                instance_uuid=inst.uuid,
                kind="restore",
                topic_code=home.code,
                added=[number],
                occurred_at=utcnow(),
                source=source or None,
                origin=_check_json(origin, "出处", _ORIGIN_MAX_BYTES),
            )
        )
        changes = await self._touch(inst, [home])
        await self.session.flush()
        return entry, home, changes

    async def _home_topic(self, inst: Instance, entry: Entry, hint: UUID | None) -> Topic:
        """恢复的条目放回哪个主题：原主题 → 提示的主题（撤销时取新条目所在的）→ 原主题拆出来的。"""
        topics = await self._topics_of(inst, [entry.topic_uuid, *([hint] if hint else [])])
        by_uuid = {t.uuid: t for t in topics}
        for candidate in (entry.topic_uuid, hint):
            t = by_uuid.get(candidate) if candidate else None
            if t is not None and t.status == "active":
                return t
        cur = by_uuid.get(entry.topic_uuid)
        seen: set[UUID] = set()
        while cur is not None and cur.split_into and cur.uuid not in seen:
            seen.add(cur.uuid)
            nxt: Topic | None = None
            for code in cur.split_into:
                found = await self._latest_topic(inst, code)
                if found is not None and found.status == "active":
                    return found
                nxt = nxt or found
            cur = nxt
        raise Conflict(
            "条目原来所在的主题已不存在，请指定恢复到哪个主题",
            code="TOPIC_REQUIRED",
            catalog=[topic_view(t) for t in await self.catalog(inst)],
        )

    # ── 彻底删除（§6.5、§7）

    async def purge_entry(self, inst: Instance, number: int) -> tuple[list[int], list[TopicChange]]:
        """彻底删除一条条目，连同它所在的整条取代链和修订历史里的记录，不可恢复。

        ★ 整条链一起删：否则用户删掉「改用 bun」之后，旧的「用 pnpm」还留在库里。
        """
        entry = await self._entry(inst, number)
        chain = await self._component(inst, entry)
        gone = {e.number for e in chain}
        topics = await self._topics_of(inst, [e.topic_uuid for e in chain])
        await self.session.execute(
            sa.delete(Entry).where(Entry.instance_uuid == inst.uuid, Entry.number.in_(gone))
        )
        await self._scrub_revisions(inst, gone)
        changes = await self._touch(inst, [t for t in topics if t.status == "active"])
        await self.session.flush()
        return sorted(gone), changes

    async def _scrub_revisions(self, inst: Instance, gone: set[int]) -> None:
        """修订历史里去掉这些编号；只剩空壳的写入 / 撤销 / 恢复记录整条删掉。

        ★ 结果副本（result）里有条目原文，碰到就清空 —— 彻底删除要连原文的副本一起删。
        """
        revisions = (
            await self.session.scalars(select(Revision).where(Revision.instance_uuid == inst.uuid))
        ).all()
        for rev in revisions:
            removed = [n for n in rev.removed or [] if n not in gone]
            added = [n for n in rev.added or [] if n not in gone]
            detail = _strip_detail(rev.detail, gone)
            if (
                removed == list(rev.removed or [])
                and added == list(rev.added or [])
                and (detail == rev.detail)
            ):
                continue
            if rev.kind in ("write", "undo", "restore") and not removed and not added:
                await self.session.delete(rev)
                continue
            rev.removed, rev.added, rev.detail, rev.result = removed, added, detail, None

    async def _component(self, inst: Instance, start: Entry) -> list[Entry]:
        """沿取代关系（前驱和后继两个方向）连起来的全部条目。"""
        found = {start.number: start}
        frontier = [start]
        while frontier:
            numbers = [e.number for e in frontier]
            successors = {e.superseded_by for e in frontier if e.superseded_by is not None}
            rows = (
                await self.session.scalars(
                    select(Entry).where(
                        Entry.instance_uuid == inst.uuid,
                        sa.or_(
                            Entry.number.in_(successors - found.keys()),
                            Entry.superseded_by.in_(numbers),
                        ),
                    )
                )
            ).all()
            frontier = [r for r in rows if r.number not in found]
            found.update((r.number, r) for r in frontier)
        return list(found.values())

    # ── 内部

    def _next_version(self, inst: Instance) -> int:
        inst.version_seq += 1
        return inst.version_seq

    async def _topics_of(self, inst: Instance, uuids: Iterable[UUID]) -> list[Topic]:
        ids = list(dict.fromkeys(uuids))
        if not ids:
            return []
        rows = await self.session.scalars(
            select(Topic).where(Topic.instance_uuid == inst.uuid, Topic.uuid.in_(ids))
        )
        return list(rows.all())

    async def _codes(self, uuids: Iterable[UUID]) -> dict[UUID, str]:
        ids = list(dict.fromkeys(uuids))
        if not ids:
            return {}
        rows = await self.session.execute(select(Topic.uuid, Topic.code).where(Topic.uuid.in_(ids)))
        return {u: c for u, c in rows.all()}

    async def _recount(self, topics: list[Topic]) -> None:
        if not topics:
            return
        await self.session.flush()
        rows = await self.session.execute(
            select(
                Entry.topic_uuid,
                func.count(),
                func.coalesce(func.sum(func.length(Entry.content)), 0),
            )
            .where(Entry.topic_uuid.in_([t.uuid for t in topics]), Entry.status == "active")
            .group_by(Entry.topic_uuid)
        )
        counts = {u: (int(n), int(c)) for u, n, c in rows.all()}
        for t in topics:
            t.entry_count, t.char_count = counts.get(t.uuid, (0, 0))

    async def _touch(
        self, inst: Instance, topics: Iterable[Topic], *, recount: bool = True
    ) -> list[TopicChange]:
        """给受影响的主题换一个新版本号（同一次操作共用一个值），按需重算大小。"""
        unique = list({t.uuid: t for t in topics}.values())
        if not unique:
            return []
        if recount:
            await self._recount([t for t in unique if t.status == "active"])
        version = self._next_version(inst)
        changes = []
        for t in unique:
            changes.append(TopicChange(t.code, t.version, version))
            t.version = version
        return changes


def _number(e: Entry) -> int:
    return e.number


def _strip_detail(detail: dict[str, Any] | None, gone: set[int]) -> dict[str, Any] | None:
    if not detail:
        return detail
    out = dict(detail)
    if isinstance(out.get("entries"), list):
        out["entries"] = [n for n in out["entries"] if n not in gone]
    if isinstance(out.get("groups"), list):
        out["groups"] = [
            {**g, "entries": [n for n in g.get("entries", []) if n not in gone]}
            for g in out["groups"]
        ]
    return out
