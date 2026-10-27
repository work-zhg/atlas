"""会话后补记（doc/memory-design.html §4.5）：补上 Agent 漏记的，最后把超限的主题拆小。

    维护者每 memory_backfill_scan_s 扫一次需要补记的会话：
      开启记忆的用户会话（不含 SubAgent 会话），空闲超过 memory_backfill_idle_s 或已归档，
      之后有过变化（updated_at 晚于上次补记时记下的），且没有在跑的 run
    每个会话（加会话级锁，避免交接维护者时两边同时做）：
      1. 只取上次补记之后新增的用户 / 助手正文（不含工具输出）
      2. 一次 LLM 调用：输入新增正文 + 现有记忆 + 本会话 Agent 已写入的记忆，输出要写入的条目
      3. 按取代限制过滤后写入：来源 backfill，时间取所依据的那条用户消息的时间（§6.4）
      4. 拆分超限的主题：每个主题一次 LLM 调用分组（锁外），记忆服务加锁提交、确认分组期间没变
      5. 记下处理到哪条消息；写入或拆分了东西就留一条结果，下次会话开始时提示用户

★ 宁缺毋滥：补记时用户不在场，拿不准就不记（提示词）。
★ 取代限制（§6.3）在代码里执行，不只靠提示词：
      用户亲自写的不能取代；本会话 Agent 刚写的不能取代；会话后补记写的可以取代。
  与受保护条目冲突的那一条整体放弃（只做新增会留下两条矛盾的记忆）。
★ 暂停期间不写入，进度照样推进：暂停期间说的话，恢复后也不补。
★ 失败不推进进度，下一轮扫描重试；连续失败 memory_backfill_max_failures 次就等会话有新变化再试，
  不反复烧 LLM。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_redislock import LockBusy, RedisLock
from sqlalchemy import or_, select

from ..db.models import MemoryBackfill, Message, Run, Thread, ThreadMemory
from ..domain.messages import KIND_CHAT
from ..domain.spec import ModelSpec
from ..domain.translator import extract_text
from ..providers.llm.modelgateway import resolve_purpose
from .client import MemoryEntry, MemoryRejected, MemoryTopic, MemoryUnavailable, ScopeRef
from .prompts import EXTRACT_PROMPT, SOURCE_LABELS, SPLIT_PROMPT
from .runtime import SOURCE_AGENT, SOURCE_BACKFILL, SOURCE_USER, MemoryRuntime

if TYPE_CHECKING:
    from ..executor.assembly import ModelBuilder

logger = logging.getLogger(__name__)

__all__ = ["BackfillResult", "Backfiller"]

#: 补记时送给模型的现有记忆上限（字符）。超出的主题只列目录、不展开条目 —— 也不往里写
_MEMORY_CHARS = 16_000
#: 只看最近这么多天有过变化的会话（再早的不会突然需要补记）
_HORIZON_DAYS = 14
_LOCK_PREFIX = "atlas:memory:backfill:"


@dataclass
class _Line:
    index: int
    role: str
    at: datetime
    text: str


@dataclass
class BackfillResult:
    written: list[dict[str, Any]] = field(default_factory=list)
    split: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None


class Backfiller:
    def __init__(
        self, runtime: MemoryRuntime, model_builder: ModelBuilder, redis_factory: Any
    ) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self._build_model = model_builder
        self._redis_factory = redis_factory

    # ── 扫描

    async def due_threads(self) -> list[UUID]:
        from ..repositories.run import _UNFINISHED

        s = self.settings
        now = datetime.now(UTC)
        busy = select(Run.thread_id).where(Run.status.in_(_UNFINISHED))
        stmt = (
            select(Thread.id)
            .outerjoin(ThreadMemory, ThreadMemory.thread_id == Thread.id)
            .where(
                Thread.memory == "on",
                Thread.parent_thread_id.is_(None),
                Thread.updated_at > now - timedelta(days=_HORIZON_DAYS),
                or_(
                    Thread.status == "archived",
                    Thread.updated_at < now - timedelta(seconds=s.memory_backfill_idle_s),
                ),
                or_(
                    ThreadMemory.backfill_seen.is_(None),
                    Thread.updated_at > ThreadMemory.backfill_seen,
                ),
                Thread.id.not_in(busy),
            )
            .order_by(Thread.updated_at)
            .limit(s.memory_backfill_batch)
        )
        async with self.runtime.sessionmaker() as session:
            return list((await session.execute(stmt)).scalars())

    async def run_due(self) -> int:
        """处理一批到期的会话，返回处理了几个。维护者调用；单个会话失败不影响其它。"""
        threads = await self.due_threads()
        if not threads:
            return 0
        redis = self._redis_factory()
        done = 0
        try:
            for thread_id in threads:
                try:
                    async with RedisLock(redis, f"{_LOCK_PREFIX}{thread_id}", ttl_s=60).hold():
                        await self.process(thread_id)
                    done += 1
                except LockBusy:
                    continue
                except Exception:
                    logger.warning("会话后补记出错 thread=%s", thread_id, exc_info=True)
        finally:
            await redis.aclose()
        return done

    # ── 一个会话

    async def process(self, thread_id: UUID) -> BackfillResult:
        async with self.runtime.sessionmaker() as session:
            thread = await session.get(Thread, thread_id)
            state = await session.get(ThreadMemory, thread_id)
        if thread is None or thread.memory == "off" or thread.parent_thread_id is not None:
            return BackfillResult(skipped="not_eligible")
        seen = thread.updated_at
        upto = state.backfill_upto if state is not None else None
        failures = state.backfill_failures if state is not None else 0
        try:
            lines, last_at = await self._new_lines(thread_id, after=upto)
            result = await self._process(thread, lines)
        except Exception as exc:
            failures += 1
            values: dict[str, Any] = {
                "backfill_failures": failures,
                "backfill_error": str(exc)[:500],
            }
            if failures >= self.settings.memory_backfill_max_failures:
                # 等会话有新变化再试（下一次变化会让 updated_at 晚于这里记下的）
                values.update(backfill_seen=seen, backfill_failures=0)
                logger.warning("会话 %s 补记连续失败 %d 次，等有新消息再试", thread_id, failures)
            await self.runtime.update_state(thread_id, **values)
            raise
        if result.written or result.split:
            async with self.runtime.sessionmaker() as session:
                session.add(
                    MemoryBackfill(
                        thread_id=thread_id,
                        user_id=thread.created_by,
                        written=result.written,
                        split=result.split,
                    )
                )
                await session.commit()
        await self.runtime.update_state(
            thread_id,
            backfill_upto=last_at or upto,
            backfill_seen=seen,
            backfill_failures=0,
            backfill_error=None,
        )
        logger.info(
            "会话后补记 thread=%s 写入=%d 拆分=%d %s",
            thread_id,
            len(result.written),
            len(result.split),
            result.skipped or "",
        )
        return result

    async def _process(self, thread: Thread, lines: list[_Line]) -> BackfillResult:
        owner = thread.created_by
        paused, _ = await self.runtime.user_setting(owner)
        if paused:
            return BackfillResult(skipped="paused")
        scope = self.runtime.user_scope(owner)
        result = BackfillResult()
        chars = sum(len(line.text) for line in lines)
        if (
            not any(line.role == "user" for line in lines)
            or chars < self.settings.memory_backfill_min_chars
        ):
            result.skipped = "no_new_content"
        else:
            result.written = await self._extract(thread, scope, lines)
        # ★ 抽取被跳过时也照常检查超限的主题（不管是谁写满的，都在这一步处理）
        result.split = await self._split_oversized(scope, owner)
        return result

    async def _new_lines(
        self, thread_id: UUID, *, after: datetime | None
    ) -> tuple[list[_Line], datetime | None]:
        """上次补记之后新增的用户 / 助手正文。工具调用与结果一律不进（体量大、信噪比低）。"""
        stmt = select(Message).where(Message.thread_id == thread_id, Message.kind == KIND_CHAT)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        async with self.runtime.sessionmaker() as session:
            rows = list((await session.execute(stmt.order_by(Message.created_at))).scalars())
        if not rows:
            return [], None
        lines: list[_Line] = []
        for row in rows:
            text = extract_text(row.content).strip()
            if text:
                lines.append(_Line(len(lines) + 1, row.role, row.created_at, text))
        # 正文太长时只留最近的部分（进度照样推进到最后一条）
        budget = self.settings.memory_backfill_input_chars
        kept: list[_Line] = []
        for line in reversed(lines):
            if budget - len(line.text) < 0 and kept:
                break
            budget -= len(line.text)
            kept.append(line)
        return list(reversed(kept)), rows[-1].created_at

    # ── 抽取

    async def _extract(
        self, thread: Thread, scope: ScopeRef, lines: list[_Line]
    ) -> list[dict[str, Any]]:
        owner = thread.created_by
        client = self.runtime.client
        catalog = await self.runtime.call(owner, lambda: client.catalog(scope, as_user=owner))
        shown: dict[str, list[MemoryEntry]] = {}
        budget = _MEMORY_CHARS
        for topic in catalog:
            if topic.char_count > budget:
                continue
            _, entries = await client.load(scope, topic.code, as_user=owner)
            shown[topic.code] = entries
            budget -= topic.char_count
        session_written, _ = await client.entries(
            scope, as_user=owner, origin={"thread": str(thread.id)}, limit=200
        )
        prompt = EXTRACT_PROMPT.format(
            today=datetime.now(UTC).date().isoformat(),
            memories=_render_memories(scope, catalog, shown, thread.id),
            session_written=_render_session(session_written),
            conversation=_render_lines(lines),
        )
        data = await self._ask_json(prompt)
        ops = data.get("writes") if isinstance(data, dict) else None
        if not isinstance(ops, list):
            raise ValueError("抽取模型的输出里没有 writes 数组")

        known_entries = {e.number: e for es in shown.values() for e in es}
        known_entries.update({e.number: e for e in session_written})
        by_index = {line.index: line for line in lines}
        topics = {t.code: t for t in catalog}
        written: list[dict[str, Any]] = []
        for op in ops:
            if len(written) >= self.settings.memory_backfill_max_writes:
                break
            item = await self._apply(
                thread, scope, op, topics=topics, shown=shown, entries=known_entries, lines=by_index
            )
            if item is not None:
                written.append(item)
        return written

    async def _apply(
        self,
        thread: Thread,
        scope: ScopeRef,
        op: Any,
        *,
        topics: dict[str, MemoryTopic],
        shown: dict[str, list[MemoryEntry]],
        entries: dict[int, MemoryEntry],
        lines: dict[int, _Line],
    ) -> dict[str, Any] | None:
        if not isinstance(op, dict):
            return None
        code = str(op.get("topic") or "").strip()
        content = str(op.get("content") or "").strip()
        evidence = lines.get(_int(op.get("evidence")))
        if not code or not content or evidence is None or evidence.role != "user":
            logger.info("补记：丢弃不完整的条目 %r", op)
            return None
        if code in topics and code not in shown:
            # 现有内容没给模型看，判断不了重复与冲突 —— 不写
            logger.info("补记：主题 %s 内容未展开，跳过 %r", code, content)
            return None
        replaces: list[int] = []
        for raw in op.get("replaces") or []:
            number = _int(raw)
            old = entries.get(number)
            if old is None:
                continue
            if old.source == SOURCE_USER or (
                old.source == SOURCE_AGENT and (old.origin or {}).get("thread") == str(thread.id)
            ):
                logger.info("补记：#%s 受保护（%s），放弃 %r", number, old.source, content)
                return None
            replaces.append(number)

        owner = thread.created_by
        client = self.runtime.client
        try:
            if code not in topics:
                new = op.get("new_topic") if isinstance(op.get("new_topic"), dict) else None
                description = str((new or {}).get("description") or "").strip()
                if not description:
                    logger.info("补记：新主题 %s 没有描述，跳过", code)
                    return None
                created = await client.create_topic(
                    scope,
                    code,
                    description,
                    attrs={"resident": (new or {}).get("resident") is True},
                    as_user=owner,
                )
                topics[code] = created
                shown[code] = []
            result = await client.write(
                scope,
                topic=code,
                content=content,
                remove=sorted(set(replaces)),
                occurred_at=evidence.at,
                source=SOURCE_BACKFILL,
                origin={"thread": str(thread.id)},
                idempotency_key=_key(thread.id, code, content, replaces),
                as_user=owner,
            )
        except MemoryRejected as exc:
            logger.info("补记：记忆服务拒绝 %s（%s），跳过 %r", exc.code, exc.message, content)
            return None
        if result.get("outcome") != "applied" or not result.get("added"):
            # 已有更新的信息：直接放弃这一条（§6.4）
            logger.info("补记：%r 被丢弃（%s）", content, result.get("conflicts"))
            return None
        added = result["added"]
        return {
            "scope": scope.name,
            "topic": code,
            "number": added.get("number"),
            "content": content,
            "removed": list(result.get("removed") or []),
            "revision": result.get("revision"),
        }

    # ── 拆分（§4.5 最后一步）

    async def _split_oversized(self, scope: ScopeRef, owner: UUID) -> list[dict[str, Any]]:
        limit = self.settings.memory_topic_max_chars
        client = self.runtime.client
        try:
            catalog = await self.runtime.call(owner, lambda: client.catalog(scope, as_user=owner))
        except (MemoryRejected, MemoryUnavailable):
            logger.warning("补记：列主题目录失败，跳过拆分", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for topic in catalog:
            if topic.char_count <= limit:
                continue
            try:
                item = await self._split_one(scope, owner, topic)
            except (MemoryUnavailable, ValueError):
                logger.warning("补记：拆分主题 %s 失败", topic.code, exc_info=True)
                continue
            if item is not None:
                out.append(item)
        return out

    async def _split_one(
        self, scope: ScopeRef, owner: UUID, topic: MemoryTopic
    ) -> dict[str, Any] | None:
        client = self.runtime.client
        current, entries = await client.load(scope, topic.code, as_user=owner)
        if len(entries) < 2:
            logger.info("补记：主题 %s 只有 %d 条，拆不开", topic.code, len(entries))
            return None
        prompt = SPLIT_PROMPT.format(
            code=current.code,
            description=current.description or "（无）",
            entries="\n".join(f"- #{e.number} {e.content}" for e in entries),
        )
        # ★ 分组在锁外进行：调 LLM 期间不挡对话中的写入；提交时记忆服务确认主题没变
        data = await self._ask_json(prompt)
        groups = data.get("groups") if isinstance(data, dict) else None
        valid = _valid_groups(groups, {e.number for e in entries})
        if valid is None:
            logger.info("补记：主题 %s 拆不开（模型没给出覆盖全部条目的分组）", topic.code)
            return None
        try:
            await client.split(
                scope,
                current.code,
                expected_version=current.version,
                groups=[{**g, "attrs": dict(current.attrs)} for g in valid],
                as_user=owner,
            )
        except MemoryRejected as exc:
            # 分组期间有新的写入，或别的会话已经拆了：本次放弃，下次补记再拆
            logger.info("补记：拆分主题 %s 被拒绝（%s），下次再拆", topic.code, exc.code)
            return None
        return {"scope": scope.name, "topic": current.code, "into": [g["code"] for g in valid]}

    # ── 模型

    async def _ask_json(self, prompt: str) -> Any:
        s = self.settings
        # 经模型网关时是平台用途「记忆补记」（doc/model-design.html §6.7），模型由模型管理员指定
        found = await resolve_purpose(
            s, None, "memory_backfill", legacy_model=s.memory_backfill_model or s.summarizer_model
        )
        if found is None:
            raise RuntimeError("平台用途「记忆补记」没有可用的模型")
        chat = self._build_model(
            ModelSpec(
                model=found.model,
                provider=found.provider,  # type: ignore[arg-type]
                max_output_tokens=2_048,
                caps=found.caps,
            ),
            base_url=found.endpoint.base_url,
            api_key=found.endpoint.api_key,
            headers=found.endpoint.headers,
        )
        # ★ 消息用 dict 传：不在 server 里新增 langchain 类型的 import（server 不依赖框架的方向）
        result = await chat.ainvoke([{"role": "user", "content": prompt}])
        return _parse_json(extract_text(result.content))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _key(thread_id: UUID, code: str, content: str, replaces: list[int]) -> str:
    raw = f"{thread_id}|{code}|{content}|{sorted(set(replaces))}"
    return "backfill:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def _parse_json(text: str) -> Any:
    """取出第一个 { 到最后一个 } 之间的 JSON（模型偶尔会包一层代码块）。"""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"模型没有输出 JSON：{text[:200]!r}")
    return json.loads(text[start : end + 1])


def _valid_groups(groups: Any, numbers: set[int]) -> list[dict[str, Any]] | None:
    if not isinstance(groups, list) or len(groups) < 2:
        return None
    out: list[dict[str, Any]] = []
    listed: list[int] = []
    for g in groups:
        if not isinstance(g, dict):
            return None
        code = str(g.get("code") or "").strip()
        members = [_int(n) for n in g.get("entries") or []]
        if not code or not members:
            return None
        out.append(
            {
                "code": code,
                "description": str(g.get("description") or "").strip(),
                "entries": members,
            }
        )
        listed += members
    if len(listed) != len(set(listed)) or set(listed) != numbers:
        return None
    if len({g["code"] for g in out}) != len(out):
        return None
    return out


def _source_label(e: MemoryEntry, thread_id: UUID) -> str:
    label = SOURCE_LABELS.get(e.source, e.source or "未知")
    if e.source == SOURCE_AGENT and (e.origin or {}).get("thread") == str(thread_id):
        label += " · 本会话"
    return label


def _render_memories(
    scope: ScopeRef,
    catalog: list[MemoryTopic],
    shown: dict[str, list[MemoryEntry]],
    thread_id: UUID,
) -> str:
    if not catalog:
        return "（还没有任何记忆）"
    parts = []
    for t in catalog:
        flag = "常驻" if t.resident else "按需"
        head = f"### {scope.label} · {t.code}（{flag}）：{t.description or '（无描述）'}"
        if t.code not in shown:
            parts.append(f"{head}\n（内容未展开：超出篇幅。不要往这个主题写）")
            continue
        body = "\n".join(
            f"- [#{e.number} {_source_label(e, thread_id)} {e.date}] {e.content}"
            for e in shown[t.code]
        )
        parts.append(f"{head}\n{body or '（空）'}")
    return "\n".join(parts)


def _render_session(entries: list[MemoryEntry]) -> str:
    if not entries:
        return "（无）"
    return "\n".join(f"- [#{e.number}] {e.content}" for e in entries)


def _render_lines(lines: list[_Line]) -> str:
    out = []
    for line in lines:
        who = "用户" if line.role == "user" else "助手"
        stamp = f"（{line.at.strftime('%Y-%m-%d %H:%M')}）" if line.role == "user" else ""
        out.append(f"[{line.index}] {who}{stamp}：{line.text}")
    return "\n".join(out)
