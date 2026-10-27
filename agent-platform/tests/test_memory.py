"""Atlas 一侧的记忆（doc/memory-design.html §4–§7）。

记忆服务用真实现：atlas_memory 的 app 经 ASGITransport 跑在进程里（它自己的测试库
atlas_memory_test），数据权限开关打开、用户中心换成只认 Owner 的替身 —— 版本号、作废的写入、
「调用用户 = 会话所属用户」都按真实规则走。Atlas 这边的状态表在 Atlas 的库里。

## 这组测试真正在守什么

  · 关联主键由会话决定（会话所属用户），工具参数里没有 —— 读写不到别人的记忆
  · 常驻块按快照复用（保住前缀缓存），只在该刷新时重建；别处的更新靠末尾的提示
  · 写之前必须看过目标主题（版本表），作废的写入如实告诉 Agent
  · 补记守住取代限制：不推翻用户亲自写的、不推翻本会话 Agent 刚写的
  · 记忆服务挂了不影响对话
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.config import Settings
from atlas_agent.db.models import MemoryBackfill, MemoryUserSetting, Message, Thread, ThreadMemory
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.domain.tool_registry import MEMORY_TOOL, tool_effects
from atlas_agent.memory import Backfiller, MemoryClient, MemoryRuntime
from atlas_engine.contracts import ToolEffect
from sqlalchemy import delete, select, text

MEMORY_DB = "atlas_memory_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
MEMORY_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{MEMORY_DB}"


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, tzinfo=UTC)


# ───────────────────────────── 记忆服务（真实现）


class OwnerOnly:
    """用户中心数据权限的替身：登记时写的 Owner 有全部权限，其他人没有。"""

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}

    async def level(self, user_id: str, data_code: str, data_id: str) -> str:
        return "OWNER" if self.owners.get(data_id) == user_id else "NONE"

    async def accessible(self, user_id: str, data_code: str, min_level: str = "READ") -> Any:
        return {d for d, u in self.owners.items() if u == user_id}, "NONE"

    async def register(self, data_code: str, data_id: str, data_name: str, owner: str) -> bool:
        if data_id in self.owners:
            return False
        self.owners[data_id] = owner
        return True

    async def cleanup(self, data_code: str, data_id: str) -> None:
        self.owners.pop(data_id, None)


@pytest.fixture(scope="module", autouse=True)
def _memory_database() -> Iterator[None]:
    """记忆服务的测试库：不存在就建，迁移到最新（同 memory/tests/conftest.py）。"""
    saved = os.environ.get("ATLAS_MEMORY_DATABASE_URL")
    os.environ["ATLAS_MEMORY_DATABASE_URL"] = MEMORY_URL
    from atlas_memory.settings import get_settings as memory_settings
    from sqlalchemy.ext.asyncio import create_async_engine

    memory_settings.cache_clear()

    async def _ensure() -> None:
        engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": MEMORY_DB}
            )
            if not exists:
                await conn.execute(text(f"CREATE DATABASE {MEMORY_DB}"))
        await engine.dispose()

    asyncio.run(_ensure())
    from atlas_memory.__main__ import migrate

    migrate()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("ATLAS_MEMORY_DATABASE_URL", None)
        else:
            os.environ["ATLAS_MEMORY_DATABASE_URL"] = saved
        memory_settings.cache_clear()


@pytest.fixture
async def memory_app() -> AsyncIterator[Any]:
    from atlas_memory.api.app import create_app as create_memory_app
    from atlas_memory.db.session import get_engine
    from atlas_memory.db.session import get_sessionmaker as memory_sessions
    from atlas_memory.scopes import ScopeService

    async with memory_sessions()() as session:
        await session.execute(
            text("TRUNCATE mem_scope, mem_instance, mem_topic, mem_entry, mem_revision")
        )
        await ScopeService(session).create(
            code="user",
            description="用户",
            key_fields=["uid"],
            check_permission=True,
            data_code=None,
            actor="test",
        )
        await session.commit()
    app = create_memory_app(perms=OwnerOnly())
    yield app
    await get_engine().dispose()
    get_engine.cache_clear()
    memory_sessions.cache_clear()


def _settings(**kw: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        litellm_key="x",
        memory_base_url="http://memory",
        memory_topic_max_chars=kw.pop("memory_topic_max_chars", 200),
        **kw,
    )


@pytest.fixture
def owners() -> list[UUID]:
    """本用例用到的用户 / 会话 id，结束时只删它们的行（不碰库里的其它数据）。"""
    return []


@pytest.fixture
async def rt(memory_app: Any, owners: list[UUID]) -> AsyncIterator[MemoryRuntime]:
    client = MemoryClient("http://memory", transport=httpx.ASGITransport(app=memory_app))
    runtime = MemoryRuntime(_settings(), get_sessionmaker(), client)
    yield runtime
    async with get_sessionmaker()() as session:
        await session.execute(delete(ThreadMemory).where(ThreadMemory.thread_id.in_(owners)))
        await session.execute(
            delete(MemoryUserSetting).where(MemoryUserSetting.user_id.in_(owners))
        )
        await session.execute(delete(MemoryBackfill).where(MemoryBackfill.user_id.in_(owners)))
        await session.execute(delete(Message).where(Message.thread_id.in_(owners)))
        await session.execute(delete(Thread).where(Thread.id.in_(owners)))
        await session.commit()


def new_thread(owners: list[UUID], owner: UUID | None = None, **kw: Any) -> SimpleNamespace:
    owner = owner or uuid4()
    thread = SimpleNamespace(
        id=uuid4(),
        memory="on",
        created_by=owner,
        compact_count=0,
        parent_thread_id=None,
        **kw,
    )
    owners += [owner, thread.id]
    return thread


async def seed(
    rt: MemoryRuntime,
    owner: UUID,
    topic: str,
    contents: list[str],
    *,
    resident: bool = False,
    when: datetime | None = None,
    source: str = "user",
    origin: dict[str, Any] | None = None,
) -> list[int]:
    """往用户实例里写：建主题（不存在时）+ 逐条写入。返回条目编号。"""
    scope = rt.user_scope(owner)
    await rt.register(owner)
    if topic not in {t.code for t in await rt.client.catalog(scope, as_user=owner)}:
        await rt.client.create_topic(
            scope, topic, f"{topic} 相关", attrs={"resident": resident}, as_user=owner
        )
    numbers = []
    for content in contents:
        r = await rt.client.write(
            scope,
            topic=topic,
            content=content,
            remove=[],
            occurred_at=when or at(9),
            source=source,
            origin=origin,
            idempotency_key=None,
            as_user=owner,
        )
        numbers.append(int(r["added"]["number"]))
    return numbers


async def turn(rt: MemoryRuntime, thread: Any, **kw: Any) -> Any:
    kw.setdefault("run_id", uuid4())
    kw.setdefault("writable", False)
    kw.setdefault("resume", False)
    kw.setdefault("history_upto", datetime.now(UTC))
    kw.setdefault("occurred_at", at(12))
    return await rt.turn(thread, **kw)


# ───────────────────────────── 常驻块与快照（§5.2、§5.3）


async def test_first_turn_builds_resident_block(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "沟通偏好", ["回复使用中文", "技术术语保留英文"], resident=True)
    await seed(rt, owner, "编码-java", ["不使用 Lombok"])
    mt = await turn(rt, thread)
    assert mt is not None
    assert "沟通偏好（常驻）" in mt.block
    assert "- 回复使用中文（2026-10-26）#1" in mt.block
    assert "编码-java：编码-java 相关（1 条" in mt.block
    assert "不使用 Lombok" not in mt.block  # 不常驻的只在目录里
    assert [t.name for t in mt.tools()] == ["load_memory"]
    state = await rt.state(thread.id)
    assert state is not None and state.registered_at is not None
    assert state.snapshot["block"] in mt.block
    assert set(state.known["user"]) == {"沟通偏好"}


async def test_snapshot_reused_and_updates_reminded(rt: MemoryRuntime, owners: list[UUID]) -> None:
    """缓存还有效时常驻块原样复用（前缀缓存不失效）；别处的更新只在末尾提示。"""
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "沟通偏好", ["回复使用中文"], resident=True)
    first = await turn(rt, thread)
    await seed(rt, owner, "沟通偏好", ["不要用表情符号"])  # 别的会话写入
    await seed(rt, owner, "工具偏好", ["JS 项目用 bun"])  # 别的会话新建
    second = await turn(rt, thread)
    assert second.block == first.block
    [(name, body)] = second.reminders()
    assert name == "memory_updates"
    assert "用户 · 沟通偏好：有更新" in body
    assert "用户 · 工具偏好：新主题" in body


@pytest.mark.parametrize("reason", ["idle", "compaction", "user"])
async def test_snapshot_refreshes_when_cache_is_gone(
    rt: MemoryRuntime, owners: list[UUID], reason: str
) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "沟通偏好", ["回复使用中文"], resident=True)
    await turn(rt, thread)
    await seed(rt, owner, "沟通偏好", ["不要用表情符号"])
    kw: dict[str, Any] = {}
    if reason == "idle":
        kw["history_upto"] = datetime.now(UTC) - timedelta(hours=2)
    elif reason == "compaction":
        thread.compact_count = 1
    else:
        await rt.bump_epoch(owner)
    refreshed = await turn(rt, thread, **kw)
    assert "不要用表情符号" in refreshed.block
    assert refreshed.reminders() == []


async def test_resume_segment_never_rebuilds(rt: MemoryRuntime, owners: list[UUID]) -> None:
    """同一个 run 的续跑段，系统提示词必须和前一段一致。"""
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "沟通偏好", ["回复使用中文"], resident=True)
    first = await turn(rt, thread)
    await rt.bump_epoch(owner)
    await seed(rt, owner, "沟通偏好", ["不要用表情符号"])
    resumed = await turn(rt, thread, resume=True)
    assert resumed.block == first.block


async def test_resident_budget_demotes_topics(rt: MemoryRuntime, owners: list[UUID]) -> None:
    rt.settings.memory_resident_budget_chars = 10
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "身份", ["后端工程师"], resident=True)
    await seed(rt, owner, "沟通偏好", ["回复使用中文，技术术语保留英文"], resident=True)
    mt = await turn(rt, thread)
    assert "身份（常驻）" in mt.block
    assert "沟通偏好：沟通偏好 相关（常驻，超出预算改为按需" in mt.block


# ───────────────────────────── 按需加载与版本表（§5.4、§5.5）


async def test_load_then_unchanged(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "编码-java", ["不使用 Lombok", "单元测试用 JUnit 5"])
    mt = await turn(rt, thread)
    loaded = await mt._load("编码-java")
    assert "主题 [用户 · 编码-java] 共 2 条" in loaded
    assert "- 不使用 Lombok（2026-10-26）#1" in loaded
    again = await mt._load("编码-java")
    assert "已在上下文中，没有变化" in again
    assert "没有主题 编码-go" in await mt._load("编码-go")


async def test_reload_clears_update_reminder(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "编码-java", ["不使用 Lombok"])
    mt = await turn(rt, thread)
    await mt._load("编码-java")
    await seed(rt, owner, "编码-java", ["单元测试用 JUnit 5"])  # 别的会话
    mt2 = await turn(rt, thread)
    assert "编码-java：有更新" in mt2.reminders()[0][1]
    await mt2._load("编码-java")
    assert mt2.reminders() == []


# ───────────────────────────── 过程中存（§4.4）


async def test_save_requires_seeing_topic_first(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "工具偏好", ["用 pnpm"])
    mt = await turn(rt, thread, writable=True)
    assert [t.name for t in mt.tools()] == ["load_memory", "save_memory", "forget_memory"]
    assert "## 记忆的写入" in mt.block
    first, artifact = await mt._save("工具偏好", "2026-10 起 JS 项目改用 bun", replaces=[1])
    assert artifact is None
    assert "这次没有保存" in first and "- 用 pnpm（2026-10-26）#1" in first
    text_, artifact = await mt._save("工具偏好", "2026-10 起 JS 项目改用 bun", replaces=[1])
    assert text_ == "已记住 #2（用户 · 工具偏好），取代了 #1。"
    assert artifact["memory"]["number"] == 2 and artifact["memory"]["removed"] == [1]
    assert artifact["memory"]["revision"]
    # 自己的写入推进了已知版本：不会被当成别处的更新
    assert mt.reminders() == []
    _, entries = await rt.client.load(rt.user_scope(owner), "工具偏好", as_user=owner)
    [entry] = entries
    assert entry.source == "agent"
    assert entry.origin == {"thread": str(thread.id), "run": str(mt.run_id)}
    assert entry.occurred_at.startswith("2026-10-26T12:00")


async def test_save_creates_topic(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    mt = await turn(rt, thread, writable=True)
    refused, _ = await mt._save("沟通偏好", "回复使用中文")
    assert "新建主题要给出 topic_description" in refused
    done, _ = await mt._save(
        "沟通偏好", "回复使用中文", topic_description="怎么回复：语言、格式", resident=True
    )
    assert done == "已记住 #1（用户 · 沟通偏好）。"
    topics = await rt.client.catalog(rt.user_scope(thread.created_by), as_user=thread.created_by)
    assert [(t.code, t.resident) for t in topics] == [("沟通偏好", True)]


async def test_save_discarded_when_newer_info_exists(rt: MemoryRuntime, owners: list[UUID]) -> None:
    """§6.4：本轮的时间早于已有条目 —— 记忆服务丢弃这次写入，Agent 被告知并要重新加载。"""
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "项目约定", ["测试命令是 make test"], when=at(13))
    mt = await turn(rt, thread, writable=True, occurred_at=at(10))
    await mt._load("项目约定")
    text_, artifact = await mt._save("项目约定", "测试命令是 tox", replaces=[1])
    assert artifact is None
    assert "已有更新的信息" in text_ and "#1 现在是 #1" in text_
    # 已知版本作废：下次保存会先把最新内容给 Agent 看
    again, _ = await mt._save("项目约定", "测试命令是 tox", replaces=[1])
    assert "这次没有保存：写入前要先看过" in again


async def test_forget(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "身份", ["用 Java"])
    mt = await turn(rt, thread, writable=True)
    text_, artifact = await mt._forget([1])
    assert text_ == "已遗忘 #1。"
    assert artifact["memory"]["action"] == "forgotten"
    assert "没有条目 #9" in (await mt._forget([9]))[0]


async def test_pause_and_write_limit(rt: MemoryRuntime, owners: list[UUID]) -> None:
    rt.settings.memory_max_writes_per_run = 1
    thread = new_thread(owners)
    mt = await turn(rt, thread, writable=True)
    await mt._save("身份", "后端工程师", topic_description="身份与背景")
    limited, _ = await mt._save("身份", "在杭州")
    assert "本轮写入次数已达上限" in limited
    await rt.set_paused(thread.created_by, True)
    paused, _ = await mt._forget([1])
    assert paused == "用户已暂停记忆写入，这次没有保存。"


async def test_subagent_is_read_only(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    thread.parent_thread_id = uuid4()
    mt = await turn(rt, thread, writable=True)
    assert [t.name for t in mt.tools()] == ["load_memory"]
    assert "## 记忆的写入" not in mt.block


async def test_memory_off_and_unavailable(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    thread.memory = "off"
    assert await turn(rt, thread) is None

    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = MemoryRuntime(
        rt.settings,
        rt.sessionmaker,
        MemoryClient("http://memory", transport=httpx.MockTransport(refuse)),
    )
    assert await turn(down, new_thread(owners)) is None


async def test_identity_is_the_session_owner(rt: MemoryRuntime, owners: list[UUID]) -> None:
    """数据权限开关打开：会话所属用户以外的人读不到这个实例（§3.7）。"""
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "身份", ["后端工程师"])
    stranger = uuid4()
    from atlas_agent.memory import MemoryRejected

    with pytest.raises(MemoryRejected) as err:
        await rt.client.catalog(rt.user_scope(owner), as_user=stranger)
    assert err.value.code == "NO_PERMISSION"


async def test_reregisters_after_instance_purged(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = new_thread(owners)
    owner = thread.created_by
    await seed(rt, owner, "身份", ["后端工程师"])
    await turn(rt, thread)
    await rt.client.purge_instance(rt.user_scope(owner), as_user=owner)
    await rt.bump_epoch(owner)
    mt = await turn(rt, thread)
    assert mt is not None and "（还没有任何记忆）" in mt.block


def test_memory_tools_skip_approval() -> None:
    """记忆的写入不走审批（当场可见、可撤销）：按 PLAN 归类。"""
    effects = tool_effects()
    assert effects["save_memory"] is ToolEffect.PLAN
    assert effects["forget_memory"] is ToolEffect.PLAN
    assert effects["load_memory"] is ToolEffect.READ
    assert MEMORY_TOOL == "memory"


# ───────────────────────────── 会话后补记（§4.5）


class FakeModel:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, spec: Any, *, base_url: str, api_key: str) -> FakeModel:
        return self

    async def ainvoke(self, messages: list[dict[str, str]]) -> Any:
        self.prompts.append(messages[-1]["content"])
        reply = self.replies.pop(0)
        return SimpleNamespace(content=reply if isinstance(reply, str) else json.dumps(reply))


async def make_session(
    owners: list[UUID], lines: list[tuple[str, str, datetime]], owner: UUID | None = None
) -> Thread:
    """Atlas 库里建一个空闲了两小时的会话和它的消息。"""
    owner = owner or uuid4()
    thread = Thread(
        id=uuid4(),
        agent_id=uuid4(),
        title="t",
        created_by=owner,
        memory="on",
        updated_at=datetime.now(UTC) - timedelta(hours=2),
    )
    owners += [owner, thread.id]
    async with get_sessionmaker()() as session:
        session.add(thread)
        for role, body, when in lines:
            session.add(
                Message(
                    thread_id=thread.id,
                    role=role,
                    kind="chat",
                    content=[{"type": "text", "text": body}],
                    created_at=when,
                )
            )
        await session.commit()
    return thread


def backfiller(rt: MemoryRuntime, model: FakeModel) -> Backfiller:
    return Backfiller(rt, model, lambda: None)


async def test_backfill_writes_what_agent_missed(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = await make_session(
        owners,
        [
            ("user", "以后回复都用中文，技术术语保留英文就行，这个请一直这样。", at(10)),
            ("assistant", "好的，明白。", at(10, 1)),
        ],
    )
    owner = thread.created_by
    await rt.register(owner)
    model = FakeModel(
        [
            {
                "writes": [
                    {
                        "topic": "沟通偏好",
                        "new_topic": {"description": "怎么回复：语言、格式", "resident": True},
                        "content": "回复使用中文，技术术语保留英文",
                        "replaces": [],
                        "evidence": 1,
                    },
                    {"topic": "沟通偏好", "content": "助手说的不算", "evidence": 2},
                ]
            }
        ]
    )
    result = await backfiller(rt, model).process(thread.id)
    assert [w["content"] for w in result.written] == ["回复使用中文，技术术语保留英文"]
    assert "[1] 用户（2026-10-26 10:00）：以后回复都用中文" in model.prompts[0]
    _, entries = await rt.client.load(rt.user_scope(owner), "沟通偏好", as_user=owner)
    [entry] = entries
    assert entry.source == "backfill"
    assert entry.occurred_at.startswith("2026-10-26T10:00")  # 原消息的时间，不是补记执行的时间
    state = await rt.state(thread.id)
    assert state is not None and state.backfill_upto == at(10, 1)
    async with get_sessionmaker()() as session:
        notes = (
            (
                await session.execute(
                    select(MemoryBackfill).where(MemoryBackfill.thread_id == thread.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(notes) == 1 and notes[0].written[0]["number"] == entry.number
    # 进度推进了：没有新内容就不再调模型（但照样检查超限主题）
    again = await backfiller(rt, FakeModel([])).process(thread.id)
    assert again.skipped == "no_new_content"


async def test_backfill_respects_protected_entries(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = await make_session(owners, [("user", "我现在改用 Go 了，Java 不怎么写了。", at(10))])
    owner = thread.created_by
    [mine] = await seed(rt, owner, "身份", ["主要写 Java"], source="user")
    [agent_now] = await seed(
        rt, owner, "身份", ["在杭州"], source="agent", origin={"thread": str(thread.id)}
    )
    [older] = await seed(rt, owner, "身份", ["用 VS Code"], source="backfill")
    model = FakeModel(
        [
            {
                "writes": [
                    {"topic": "身份", "content": "主要写 Go", "replaces": [mine], "evidence": 1},
                    {"topic": "身份", "content": "在上海", "replaces": [agent_now], "evidence": 1},
                    {"topic": "身份", "content": "用 GoLand", "replaces": [older], "evidence": 1},
                ]
            }
        ]
    )
    result = await backfiller(rt, model).process(thread.id)
    assert [(w["content"], w["removed"]) for w in result.written] == [("用 GoLand", [older])]
    assert f"[#{mine} 用户亲自" in model.prompts[0]
    assert "Agent 写入 · 本会话" in model.prompts[0]


async def test_backfill_paused_advances_without_writing(
    rt: MemoryRuntime, owners: list[UUID]
) -> None:
    thread = await make_session(owners, [("user", "记住：我讨厌在回复里看到表情符号。", at(10))])
    await rt.set_paused(thread.created_by, True)
    result = await backfiller(rt, FakeModel([])).process(thread.id)
    assert result.skipped == "paused"
    state = await rt.state(thread.id)
    assert state is not None and state.backfill_upto == at(10)


async def test_backfill_splits_oversized_topic(rt: MemoryRuntime, owners: list[UUID]) -> None:
    rt.settings.memory_topic_max_chars = 20
    thread = await make_session(owners, [("assistant", "好", at(10))])
    owner = thread.created_by
    numbers = await seed(
        rt,
        owner,
        "编码",
        ["Java 偏好构造器注入", "JS 项目用 bun 管理依赖", "Java 不使用 Lombok"],
        resident=True,
    )
    model = FakeModel(
        [
            {
                "groups": [
                    {
                        "code": "编码-java",
                        "description": "Java",
                        "entries": [numbers[0], numbers[2]],
                    },
                    {"code": "编码-js", "description": "JS", "entries": [numbers[1]]},
                ]
            }
        ]
    )
    result = await backfiller(rt, model).process(thread.id)
    assert result.split == [{"scope": "user", "topic": "编码", "into": ["编码-java", "编码-js"]}]
    topics = await rt.client.catalog(rt.user_scope(owner), as_user=owner)
    assert [(t.code, t.resident) for t in topics] == [("编码-java", True), ("编码-js", True)]


async def test_backfill_unsplittable_topic_is_left_alone(
    rt: MemoryRuntime, owners: list[UUID]
) -> None:
    rt.settings.memory_topic_max_chars = 10
    thread = await make_session(owners, [("assistant", "好", at(10))])
    owner = thread.created_by
    await seed(rt, owner, "工具", ["包管理用 pnpm", "构建用 vite"])
    result = await backfiller(rt, FakeModel([{"groups": []}])).process(thread.id)
    assert result.split == []
    topics = await rt.client.catalog(rt.user_scope(owner), as_user=owner)
    assert [t.code for t in topics] == ["工具"]


async def test_backfill_failures_back_off(rt: MemoryRuntime, owners: list[UUID]) -> None:
    rt.settings.memory_backfill_max_failures = 2
    thread = await make_session(owners, [("user", "记住：周报每周五下午四点前发。", at(10))])
    await rt.register(thread.created_by)
    with pytest.raises(ValueError):
        await backfiller(rt, FakeModel(["不是 JSON"])).process(thread.id)
    state = await rt.state(thread.id)
    assert state is not None and state.backfill_failures == 1 and state.backfill_seen is None
    with pytest.raises(ValueError):
        await backfiller(rt, FakeModel(["还不是"])).process(thread.id)
    state = await rt.state(thread.id)
    assert state is not None and state.backfill_failures == 0
    assert state.backfill_seen is not None  # 等会话有新变化再试
    assert state.backfill_upto is None  # 进度没有推进


async def test_due_threads(rt: MemoryRuntime, owners: list[UUID]) -> None:
    thread = await make_session(owners, [("user", "记住：周报每周五发。", at(10))])
    filler = backfiller(rt, FakeModel([{"writes": []}]))
    assert thread.id in await filler.due_threads()
    await filler.process(thread.id)
    assert thread.id not in await filler.due_threads()


# ───────────────────────────── 管理接口（§7）


@pytest.fixture
async def api(rt: MemoryRuntime) -> AsyncIterator[httpx.AsyncClient]:
    from atlas_agent.main import create_app

    from tests.fake_uc import AS_USER

    app = create_app()
    app.state.memory = rt
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AS_USER
    ) as client:
        yield client


async def test_manage_api(api: httpx.AsyncClient, owners: list[UUID]) -> None:
    from tests.fake_uc import TEST_USER

    owners.append(UUID(TEST_USER))
    r = await api.get("/v1/memories/topics")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []
    r = await api.post(
        "/v1/memories/entries",
        json={"topic": "工具偏好", "content": "用 pnpm", "topic_description": "工具与命令"},
    )
    assert r.status_code == 201, r.text
    first = r.json()["number"]
    r = await api.patch(f"/v1/memories/entries/{first}", json={"content": "改用 bun"})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["removed"] == [first]
    detail = (await api.get(f"/v1/memories/entries/{second['number']}")).json()
    assert [e["number"] for e in detail["chain"]] == [first, second["number"]]
    assert detail["entry"]["source"] == "user"
    r = await api.post(f"/v1/memories/revisions/{second['revision']}/undo")
    assert r.status_code == 204, r.text
    topic = (await api.get("/v1/memories/topics/工具偏好")).json()
    assert [e["content"] for e in topic["entries"]] == ["用 pnpm"]
    r = await api.patch("/v1/memories/topics/工具偏好", json={"resident": True})
    assert r.json()["resident"] is True
    r = await api.delete(f"/v1/memories/entries/{first}")
    assert r.status_code == 200 and first in r.json()["purged"]
    r = await api.put("/v1/memories/settings", json={"paused": True})
    assert r.json() == {"paused": True, "enabled": True}
    assert (await api.get("/v1/memories/backfills")).json() == {"data": []}
    assert (await api.delete("/v1/memories")).status_code == 204


async def test_manage_api_without_memory_service() -> None:
    from atlas_agent.main import create_app

    from tests.fake_uc import AS_USER

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test", headers=AS_USER
    ) as client:
        r = await client.get("/v1/memories/topics")
    assert r.status_code == 503
    assert r.json()["error"]["kind"] == "memory_not_enabled"
