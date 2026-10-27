"""Atlas 怎么取、怎么在对话中存（doc/memory-design.html §4.1、§4.4、§5）。

    MemoryRuntime   进程级：登记实例归属、读写会话的记忆状态（thread_memory）、为一轮组装 MemoryTurn
    MemoryTurn      一轮（一段 run）：常驻块、更新提示、load_memory / save_memory / forget_memory

## 常驻块与快照（§5.2、§5.3）

常驻块放在系统提示词里，按会话存成快照，只在缓存本来就会失效、或者用户明确做了操作时重建：

    还没有快照 / 会话发生过压缩 / 距上次回复超过缓存有效期 / 用户在管理界面操作过（epoch 变了）

重建时比对版本号：没变的常驻主题沿用快照里的条目，变了的才重新拉取。续跑段（resume）
永不重建 —— 同一个 run 的系统提示词必须前后一致。Agent 在本会话里写入不触发重建。

## 版本表（§5.5）

known[作用域][主题] = Agent 在上下文里看到的内容（常驻块 + 对话历史里的加载、写入结果）是哪个
版本。和服务端的当前版本比：

    Run 开始       列主题目录。不是刷新时机时，在请求末尾附一条「哪些主题有更新」
                   （只列主题，不带内容）
    加载主题       版本一致 → 只说「已在上下文中，没有变化」；不一致 → 返回全部条目并记下版本
    保存记忆       版本不一致（或没看过）→ 不写入，先把最新条目返回给 Agent，由它重新判断（§4.4）
    写入之后       写入前的版本等于已知版本 → 已知版本推进到写入后；否则保持原值（中间有别人写过）

★ 记忆**绝不能成为会话的硬依赖**：登记、列目录失败时本轮不用记忆（不加载、不写入），对话照常。
★ 关联主键由会话确定（会话所属用户），工具参数里没有这一项（§4.8）。
★ SubAgent 只读：有常驻块和 load_memory，没有写入工具（§4.8）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..db.models import MemoryUserSetting, ThreadMemory
from .client import (
    MemoryClient,
    MemoryEntry,
    MemoryRejected,
    MemoryTopic,
    MemoryUnavailable,
    ScopeRef,
)
from .render import render_block, render_topic, render_updates, write_guidance

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..config import Settings
    from ..db.models import Thread

logger = logging.getLogger(__name__)

__all__ = [
    "FORGET_MEMORY_TOOL",
    "LOAD_MEMORY_TOOL",
    "SAVE_MEMORY_TOOL",
    "SOURCE_AGENT",
    "SOURCE_BACKFILL",
    "SOURCE_USER",
    "MemoryRuntime",
    "MemoryTurn",
]

LOAD_MEMORY_TOOL = "load_memory"
SAVE_MEMORY_TOOL = "save_memory"
FORGET_MEMORY_TOOL = "forget_memory"

#: 条目的来源标签（§6.3）：可信度从高到低 用户亲自 → Agent 写入 → 会话后补记
SOURCE_USER = "user"
SOURCE_AGENT = "agent"
SOURCE_BACKFILL = "backfill"

T = TypeVar("T")


class _Known:
    """{作用域: {主题: 版本}} 的小包装。"""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.data: dict[str, dict[str, int]] = {
            str(s): {str(c): int(v) for c, v in (codes or {}).items()}
            for s, codes in (raw or {}).items()
        }

    def get(self, scope: str, code: str) -> int | None:
        return self.data.get(scope, {}).get(code)

    def set(self, scope: str, code: str, version: int) -> None:
        self.data.setdefault(scope, {})[code] = version

    def drop(self, scope: str, code: str) -> None:
        self.data.get(scope, {}).pop(code, None)

    def codes(self, scope: str) -> set[str]:
        return set(self.data.get(scope, {}))

    def to_json(self) -> dict[str, dict[str, int]]:
        return {s: dict(c) for s, c in self.data.items()}


class MemoryRuntime:
    """进程级。构造便宜（只持有客户端与会话工厂），但仍随执行器建一个复用。"""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: MemoryClient,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.client = client

    # ── 作用域

    def scopes_for(self, owner: UUID) -> list[ScopeRef]:
        """本会话用到的作用域实例。本期只用「用户」（§4.1；「用户项目」等 Q1 定了项目再接）。"""
        return [
            ScopeRef(
                name="user",
                code=self.settings.memory_user_scope,
                key={"uid": str(owner)},
                label="用户",
            )
        ]

    def user_scope(self, owner: UUID) -> ScopeRef:
        return self.scopes_for(owner)[0]

    # ── 登记归属（§3.7、§4.1）

    async def register(self, owner: UUID) -> None:
        for scope in self.scopes_for(owner):
            await self.client.register(scope, owner=owner)

    async def register_thread(self, thread_id: UUID, owner: UUID) -> bool:
        """建会话时登记本会话用到的实例。失败不阻塞会话（返回 False，下个 Run 开始时再登记）。"""
        try:
            await self.register(owner)
        except (MemoryUnavailable, MemoryRejected) as exc:
            logger.warning("记忆实例登记失败，本会话暂时不用记忆 thread=%s：%s", thread_id, exc)
            return False
        await self.update_state(thread_id, registered_at=datetime.now(UTC))
        return True

    async def call(self, owner: UUID, fn: Callable[[], Awaitable[T]]) -> T:
        """调记忆服务；实例还没登记（或被整个删除过）就先登记再试一次。"""
        try:
            return await fn()
        except MemoryRejected as exc:
            if exc.code != "NOT_REGISTERED":
                raise
        await self.register(owner)
        return await fn()

    # ── 状态

    async def state(self, thread_id: UUID) -> ThreadMemory | None:
        async with self.sessionmaker() as session:
            return await session.get(ThreadMemory, thread_id)

    async def update_state(self, thread_id: UUID, **values: Any) -> None:
        await self._upsert(
            ThreadMemory,
            ThreadMemory.thread_id == thread_id,
            values,
            insert={"thread_id": thread_id, **values},
        )

    async def _upsert(
        self, model: Any, where: Any, values: dict[str, Any], *, insert: dict[str, Any]
    ) -> None:
        """先改、改不到再插；并发插入撞了主键就再改一次。

        ★ 不用 PG 的 ON CONFLICT：Atlas 的库要兼容 MySQL（db/models.py 的约定）。
        """
        async with self.sessionmaker() as session:
            result = await session.execute(update(model).where(where).values(**values))
            if result.rowcount:  # type: ignore[attr-defined]
                await session.commit()
                return
            session.add(model(**insert))
            try:
                await session.commit()
                return
            except IntegrityError:
                await session.rollback()
            await session.execute(update(model).where(where).values(**values))
            await session.commit()

    async def save_known(self, thread_id: UUID, known: dict[str, Any]) -> None:
        await self.update_state(thread_id, known=known)

    async def user_setting(self, user_id: UUID) -> tuple[bool, int]:
        """(是否暂停写入, 管理操作计数)。"""
        async with self.sessionmaker() as session:
            row = await session.get(MemoryUserSetting, user_id)
        return (row.paused, row.epoch) if row is not None else (False, 0)

    async def bump_epoch(self, user_id: UUID) -> None:
        """用户在管理界面做了影响记忆的操作：各会话的常驻块在下一轮刷新（§5.3）。"""
        await self._upsert(
            MemoryUserSetting,
            MemoryUserSetting.user_id == user_id,
            {"epoch": MemoryUserSetting.epoch + 1},
            insert={"user_id": user_id, "epoch": 1},
        )

    async def set_paused(self, user_id: UUID, paused: bool) -> None:
        """暂停 / 恢复两条写入路径（§7）。已有记忆照常常驻、照常可以加载。"""
        await self._upsert(
            MemoryUserSetting,
            MemoryUserSetting.user_id == user_id,
            {"paused": paused},
            insert={"user_id": user_id, "paused": paused},
        )

    # ── 一轮

    async def turn(
        self,
        thread: Thread,
        *,
        run_id: UUID,
        writable: bool,
        resume: bool,
        history_upto: datetime | None,
        occurred_at: datetime | None,
    ) -> MemoryTurn | None:
        """为这一段 run 组装记忆。会话关了记忆、或记忆服务不可用时返回 None（本轮不用记忆）。"""
        if thread.memory == "off":
            return None
        owner = thread.created_by
        scopes = self.scopes_for(owner)
        state = await self.state(thread.id)
        _, epoch = await self.user_setting(owner)
        try:
            if state is None or state.registered_at is None:
                await self.register(owner)
                await self.update_state(thread.id, registered_at=datetime.now(UTC))
            catalogs = {
                s.name: await self.call(owner, lambda s=s: self.client.catalog(s, as_user=owner))
                for s in scopes
            }
        except (MemoryUnavailable, MemoryRejected) as exc:
            logger.warning("记忆暂不可用，本轮不加载、不写入 thread=%s：%s", thread.id, exc)
            return None

        snapshot = dict(state.snapshot or {}) if state is not None else {}
        known = _Known(state.known if state is not None else None)
        reason = self._refresh_reason(
            snapshot, thread=thread, scopes=scopes, epoch=epoch, resume=resume,
            history_upto=history_upto,
        )  # fmt: skip
        if reason is not None:
            try:
                snapshot, known = await self._rebuild(
                    thread, scopes, catalogs, snapshot, known, epoch=epoch, reason=reason
                )
            except (MemoryUnavailable, MemoryRejected) as exc:
                logger.warning("重建常驻块失败，本轮不用记忆 thread=%s：%s", thread.id, exc)
                return None
            await self.update_state(thread.id, snapshot=snapshot, known=known.to_json())
            logger.info("记忆常驻块已重建 thread=%s 原因=%s", thread.id, reason)

        block = str(snapshot.get("block") or "")
        writable = writable and thread.parent_thread_id is None
        if writable:
            block += "\n\n" + write_guidance(
                topic_max_chars=self.settings.memory_topic_max_chars,
                max_writes=self.settings.memory_max_writes_per_run,
            )
        return MemoryTurn(
            runtime=self,
            thread_id=thread.id,
            owner=owner,
            run_id=run_id,
            scopes=scopes,
            catalogs=catalogs,
            snapshot_catalog={s: dict(c) for s, c in dict(snapshot.get("catalog") or {}).items()},
            known=known,
            block=block,
            writable=writable,
            occurred_at=occurred_at or datetime.now(UTC),
        )

    def _refresh_reason(
        self,
        snapshot: dict[str, Any],
        *,
        thread: Thread,
        scopes: list[ScopeRef],
        epoch: int,
        resume: bool,
        history_upto: datetime | None,
    ) -> str | None:
        if not snapshot:
            return "initial"
        if resume:
            return None
        if int(snapshot.get("compact_count", -1)) != thread.compact_count:
            return "compaction"
        if snapshot.get("scopes") != [s.name for s in scopes]:
            return "scopes"
        if int(snapshot.get("epoch", -1)) != epoch:
            return "user"
        if history_upto is not None:
            idle = (datetime.now(UTC) - history_upto).total_seconds()
            if idle > self.settings.prompt_cache_ttl_s:
                return "idle"
        return None

    async def _rebuild(
        self,
        thread: Thread,
        scopes: list[ScopeRef],
        catalogs: dict[str, list[MemoryTopic]],
        old: dict[str, Any],
        known: _Known,
        *,
        epoch: int,
        reason: str,
    ) -> tuple[dict[str, Any], _Known]:
        """重建常驻块。没变的常驻主题沿用快照里的条目，变了的才重新拉取。"""
        owner = thread.created_by
        budget = self.settings.memory_resident_budget_chars
        old_resident = dict(old.get("resident") or {})
        resident_out: dict[str, dict[str, Any]] = {}
        sections = []
        for scope in scopes:
            shown: dict[str, list[MemoryEntry]] = {}
            demoted: set[str] = set()
            kept = dict(old_resident.get(scope.name) or {})
            for topic in catalogs[scope.name]:
                if not topic.resident:
                    continue
                cached = kept.get(topic.code)
                if cached is not None and int(cached.get("version", -1)) == topic.version:
                    entries = [MemoryEntry.of(e) for e in cached.get("entries") or []]
                else:
                    _, entries = await self.client.load(scope, topic.code, as_user=owner)
                size = sum(len(e.content) for e in entries)
                if size > budget:
                    demoted.add(topic.code)
                    continue
                budget -= size
                shown[topic.code] = entries
            resident_out[scope.name] = {
                code: {
                    "version": next(t.version for t in catalogs[scope.name] if t.code == code),
                    "entries": [e.to_json() for e in entries],
                }
                for code, entries in shown.items()
            }
            sections.append((scope, catalogs[scope.name], shown, demoted))

        # 已知版本：压缩（或首次）之后，对话历史里的加载结果都没了，只剩常驻块；否则保留
        # 历史里加载过的主题，去掉已经不存在的
        fresh = _Known() if reason in ("initial", "compaction") else known
        for scope in scopes:
            current = {t.code for t in catalogs[scope.name]}
            for code in fresh.codes(scope.name) - current:
                fresh.drop(scope.name, code)
            for code, item in resident_out[scope.name].items():
                fresh.set(scope.name, code, int(item["version"]))

        snapshot = {
            "block": render_block(sections),
            "catalog": {s.name: {t.code: t.version for t in catalogs[s.name]} for s in scopes},
            "resident": resident_out,
            "scopes": [s.name for s in scopes],
            "epoch": epoch,
            "compact_count": thread.compact_count,
            "built_at": datetime.now(UTC).isoformat(),
        }
        return snapshot, fresh


# ───────────────────────────── 一轮


class _LoadArgs(BaseModel):
    topic: str = Field(description="主题编码，取自主题目录")
    scope: str = Field(default="user", description="作用域：user（用户）")


class _SaveArgs(BaseModel):
    topic: str = Field(description="写到哪个主题（编码）。先从主题目录里选，都不合适才新建")
    content: str = Field(
        description="要记住的一个事实：陈述句，单独拿出来也能看懂，日期写成绝对日期"
    )
    replaces: list[int] = Field(
        default_factory=list,
        description="被这条新事实取代的旧条目编号（#后面的数字）；也可以顺带列出重复、过时的条目",
    )
    topic_description: str | None = Field(
        default=None,
        description="新建主题时必填：这个主题收什么、不收什么。主题已存在时不用给",
    )
    resident: bool | None = Field(
        default=None,
        description="新建主题时：是否常驻（每次对话都用得上的才常驻，如身份、沟通偏好）",
    )
    scope: str = Field(default="user", description="作用域：user（用户）")


class _ForgetArgs(BaseModel):
    numbers: list[int] = Field(description="要遗忘的条目编号（#后面的数字）")
    scope: str = Field(default="user", description="作用域：user（用户）")


@dataclass
class MemoryTurn:
    runtime: MemoryRuntime
    thread_id: UUID
    owner: UUID
    run_id: UUID
    scopes: list[ScopeRef]
    #: Run 开始时的主题目录
    catalogs: dict[str, list[MemoryTopic]]
    #: 常驻块里的主题目录 {作用域: {主题: 版本}}
    snapshot_catalog: dict[str, dict[str, int]]
    known: _Known
    #: 放进系统提示词的文本（常驻块 + 写入指引）
    block: str
    writable: bool
    #: 写入时间：这一轮用户发话的时间（§6.4）
    occurred_at: datetime
    writes: int = 0
    #: 服务端的最新版本（Run 开始时的目录，加上本轮加载、写入的结果）
    server: _Known = field(default_factory=_Known)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        for name, topics in self.catalogs.items():
            for t in topics:
                self.server.set(name, t.code, t.version)

    # ── 更新提示（§5.5）

    def reminders(self) -> list[tuple[str, str]]:
        """每次调模型时取一次：Agent 重新加载过的主题不再列出。"""
        lines: list[str] = []
        for scope in self.scopes:
            current = {t.code for t in self.catalogs.get(scope.name, [])}
            snap = set(self.snapshot_catalog.get(scope.name, {}))
            known = self.known.codes(scope.name)
            for code in sorted(current):
                seen = self.known.get(scope.name, code)
                if seen is not None:
                    if seen != self.server.get(scope.name, code):
                        lines.append(f"{scope.label} · {code}：有更新")
                elif code not in snap:
                    lines.append(f"{scope.label} · {code}：新主题")
            for code in sorted((known | snap) - current):
                lines.append(f"{scope.label} · {code}：已拆分或删除")
        return [("memory_updates", render_updates(lines))] if lines else []

    # ── 工具

    def tools(self) -> list[StructuredTool]:
        out = [
            StructuredTool.from_function(
                coroutine=self._load,
                name=LOAD_MEMORY_TOOL,
                description=(
                    "加载一个记忆主题的全部条目。主题目录在系统提示词的 <memory> 里；"
                    "开始一类工作前（例如写 Java 代码前），加载与之相关的主题。"
                    "常驻主题的内容已经在上下文里，不用再加载。"
                ),
                args_schema=_LoadArgs,
                metadata={"readOnlyHint": True},
            )
        ]
        if self.writable:
            out += [
                StructuredTool.from_function(
                    coroutine=self._save,
                    name=SAVE_MEMORY_TOOL,
                    description=(
                        "保存一条关于这位用户的长期记忆（跨会话有效），可同时取代旧条目。"
                        "写之前要看过目标主题的全部条目：没看过或主题有更新时，这次不会写入，"
                        "而是先返回最新内容，请据此判断后再调用。"
                        "写入规则见 <memory> 之后的写入指引。"
                    ),
                    args_schema=_SaveArgs,
                    response_format="content_and_artifact",
                ),
                StructuredTool.from_function(
                    coroutine=self._forget,
                    name=FORGET_MEMORY_TOOL,
                    description=(
                        "遗忘记忆条目：用户说某条记忆不对、或明确要求忘掉时使用。"
                        "遗忘可以撤销，不是彻底删除。"
                    ),
                    args_schema=_ForgetArgs,
                    response_format="content_and_artifact",
                ),
            ]
        return out

    def _scope(self, name: str) -> ScopeRef | None:
        return next((s for s in self.scopes if s.name == name), None)

    def _catalog_hint(self, scope: ScopeRef) -> str:
        codes = [t.code for t in self.catalogs.get(scope.name, [])]
        return "现有主题：" + ("、".join(codes) if codes else "（还没有）")

    async def _remember(self, scope: str, code: str, version: int) -> None:
        self.known.set(scope, code, version)
        await self.runtime.save_known(self.thread_id, self.known.to_json())

    async def _apply(self, scope: ScopeRef, changed: list[dict[str, Any]]) -> None:
        """写入结果的版本号：写入前 = 已知 → 已知推进到写入后；否则保持原值（中间有别人写过）。"""
        dirty = False
        for c in changed:
            code = str(c["topic"])
            before, after = int(c["version_before"]), int(c["version_after"])
            self.server.set(scope.name, code, after)
            if self.known.get(scope.name, code) == before:
                self.known.set(scope.name, code, after)
                dirty = True
        if dirty:
            await self.runtime.save_known(self.thread_id, self.known.to_json())

    async def _load(self, topic: str, scope: str = "user") -> str:
        ref = self._scope(scope)
        if ref is None:
            return f"没有作用域 {scope}。可用的作用域：" + "、".join(s.name for s in self.scopes)
        try:
            found, entries = await self.runtime.client.load(ref, topic, as_user=self.owner)
        except MemoryRejected as exc:
            return self._rejected_text(ref, topic, exc)
        except MemoryUnavailable:
            logger.warning("加载记忆主题失败", exc_info=True)
            return "记忆服务暂时不可用，这次没能加载。"
        self.server.set(ref.name, found.code, found.version)
        if self.known.get(ref.name, found.code) == found.version:
            return (
                f"主题 [{ref.label} · {found.code}] 已在上下文中，没有变化"
                f"（共 {len(entries)} 条）。"
            )
        await self._remember(ref.name, found.code, found.version)
        return render_topic(ref, found, entries)

    def _rejected_text(self, ref: ScopeRef, topic: str, exc: MemoryRejected) -> str:
        if exc.code == "TOPIC_GONE":
            into = "、".join(exc.context.get("split_into") or [])
            return f"主题 {topic} 已被拆分{('为 ' + into) if into else '或删除'}，请改用新的主题。"
        if exc.code == "TOPIC_NOT_FOUND":
            return f"没有主题 {topic}。{self._catalog_hint(ref)}"
        logger.warning("记忆服务拒绝了请求：%s %s", exc.code, exc.message)
        return f"记忆服务拒绝了这次操作：{exc.message}"

    async def _guard(self) -> str | None:
        paused, _ = await self.runtime.user_setting(self.owner)
        if paused:
            return "用户已暂停记忆写入，这次没有保存。"
        if self.writes >= self.runtime.settings.memory_max_writes_per_run:
            return f"本轮写入次数已达上限（{self.writes} 次），这次没有保存。"
        return None

    def _idempotency_key(self, *parts: Any) -> str:
        raw = "|".join(str(p) for p in (self.run_id, *parts))
        return "agent:" + hashlib.sha256(raw.encode()).hexdigest()[:40]

    async def _save(
        self,
        topic: str,
        content: str,
        replaces: list[int] | None = None,
        topic_description: str | None = None,
        resident: bool | None = None,
        scope: str = "user",
    ) -> tuple[str, dict[str, Any] | None]:
        ref = self._scope(scope)
        if ref is None:
            return f"没有作用域 {scope}。", None
        content = (content or "").strip()
        if not content:
            return "content 不能为空。", None
        removes = sorted(set(replaces or []))
        async with self._lock:
            if (refusal := await self._guard()) is not None:
                return refusal, None
            client = self.runtime.client
            try:
                try:
                    found, entries = await client.load(ref, topic, as_user=self.owner)
                except MemoryRejected as exc:
                    if exc.code != "TOPIC_NOT_FOUND":
                        return self._rejected_text(ref, topic, exc), None
                    if not (topic_description or "").strip():
                        return (
                            f"没有主题 {topic}。新建主题要给出 topic_description"
                            f"（说明收什么、不收什么）；{self._catalog_hint(ref)}",
                            None,
                        )
                    found = await client.create_topic(
                        ref,
                        topic,
                        topic_description.strip(),
                        attrs={"resident": bool(resident)},
                        as_user=self.owner,
                    )
                    entries = []
                    self.server.set(ref.name, found.code, found.version)
                    await self._remember(ref.name, found.code, found.version)
                self.server.set(ref.name, found.code, found.version)
                if self.known.get(ref.name, found.code) != found.version:
                    # §4.4：写之前必须看过目标主题的最新内容
                    await self._remember(ref.name, found.code, found.version)
                    return (
                        "这次没有保存：写入前要先看过该主题的最新内容（本会话还没看过，或之后有更新）。"
                        "当前内容如下，请据此判断是否重复或冲突，再重新调用 save_memory：\n"
                        + render_topic(ref, found, entries),
                        None,
                    )
                self.writes += 1
                result = await client.write(
                    ref,
                    topic=found.code,
                    content=content,
                    remove=removes,
                    occurred_at=self.occurred_at,
                    source=SOURCE_AGENT,
                    origin={"thread": str(self.thread_id), "run": str(self.run_id)},
                    idempotency_key=self._idempotency_key(ref.name, found.code, content, removes),
                    as_user=self.owner,
                )
            except MemoryRejected as exc:
                return self._rejected_text(ref, topic, exc), None
            except MemoryUnavailable:
                logger.warning("保存记忆失败", exc_info=True)
                return "记忆服务暂时不可用，这次没有保存。", None

            if result.get("outcome") == "discarded":
                self.known.drop(ref.name, found.code)
                await self.runtime.save_known(self.thread_id, self.known.to_json())
                return (
                    "这次没有保存：要取代的条目已有更新的信息"
                    f"（{_conflicts(result)}）。请先用 load_memory 重新查看该主题，再判断。",
                    None,
                )
            await self._apply(ref, list(result.get("changed") or []))
            added = result.get("added") or {}
            removed = list(result.get("removed") or [])
            text = f"已记住 #{added.get('number')}（{ref.label} · {found.code}）"
            if removed:
                text += "，取代了 " + "、".join(f"#{n}" for n in removed)
            size = sum(len(e.content) for e in entries if e.number not in removed) + len(content)
            limit = self.runtime.settings.memory_topic_max_chars
            if size > limit:
                text += (
                    f"。该主题约有 {size} 字，超过了上限 {limit}："
                    "可以合并重复、过时的条目，或拆成更细的主题"
                )
            artifact = {
                "memory": {
                    "action": "saved",
                    "scope": ref.name,
                    "topic": found.code,
                    "number": added.get("number"),
                    "content": content,
                    "removed": removed,
                    "revision": result.get("revision"),
                }
            }
            return text + "。", artifact

    async def _forget(
        self, numbers: list[int], scope: str = "user"
    ) -> tuple[str, dict[str, Any] | None]:
        ref = self._scope(scope)
        if ref is None:
            return f"没有作用域 {scope}。", None
        targets = sorted(set(numbers or []))
        if not targets:
            return "numbers 不能为空。", None
        async with self._lock:
            if (refusal := await self._guard()) is not None:
                return refusal, None
            self.writes += 1
            try:
                result = await self.runtime.client.write(
                    ref,
                    topic=None,
                    content=None,
                    remove=targets,
                    occurred_at=self.occurred_at,
                    source=SOURCE_AGENT,
                    origin={"thread": str(self.thread_id), "run": str(self.run_id)},
                    idempotency_key=self._idempotency_key(ref.name, "forget", targets),
                    as_user=self.owner,
                )
            except MemoryRejected as exc:
                if exc.code == "ENTRY_NOT_FOUND":
                    return f"没有条目 #{exc.context.get('number')}。", None
                return self._rejected_text(ref, "", exc), None
            except MemoryUnavailable:
                logger.warning("遗忘记忆失败", exc_info=True)
                return "记忆服务暂时不可用，这次没有遗忘。", None
        if result.get("outcome") == "discarded":
            return (
                f"这次没有遗忘：这些条目已有更新的信息（{_conflicts(result)}）。"
                "请先用 load_memory 重新查看再判断。",
                None,
            )
        await self._apply(ref, list(result.get("changed") or []))
        removed = list(result.get("removed") or [])
        if not removed:
            return "这些条目已经被遗忘过了。", None
        artifact = {
            "memory": {
                "action": "forgotten",
                "scope": ref.name,
                "removed": removed,
                "revision": result.get("revision"),
            }
        }
        return "已遗忘 " + "、".join(f"#{n}" for n in removed) + "。", artifact


def _conflicts(result: dict[str, Any]) -> str:
    parts = []
    for c in result.get("conflicts") or []:
        cur = f"现在是 #{c['current']}" if c.get("current") else "已被遗忘"
        parts.append(f"#{c['number']} {cur}，时间 {str(c.get('current_at') or '')[:16]}")
    return "；".join(parts)
