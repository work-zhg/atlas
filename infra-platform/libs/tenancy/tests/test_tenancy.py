"""租户隔离库的行为测试（c4-model/infra-platform/code-tenancy.html）。

自动过滤用内存 SQLite 的同步会话测：事件挂在 Session 类上，异步会话里包着的也是它。
分批删除要 AsyncSession，连真库测：只在库名以 _test 结尾时跑（防止误删开发库）。
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from atlas_tenancy import (
    SystemReason,
    TenantDataPurge,
    TenantNaming,
    TenantOwned,
    TenantScope,
    TenantSweep,
    TenantViolation,
    tenant_dependency,
    tenant_headers,
)
from fastapi import Depends, FastAPI, Request
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

A = UUID("00000000-0000-0000-0000-00000000000a")
B = UUID("00000000-0000-0000-0000-00000000000b")


class Base(DeclarativeBase):
    pass


class Agent(TenantOwned, Base):
    __tablename__ = "t_agent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Thread(TenantOwned, Base):
    __tablename__ = "t_thread"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int]
    title: Mapped[str] = mapped_column(default="")


class Catalog(Base):
    """不是租户表：没有上下文也能查。"""

    __tablename__ = "t_catalog"
    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str]


@pytest.fixture
def session() -> Session:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    with TenantScope.system(SystemReason.OPS):
        s.add_all(
            [
                Agent(id=1, name="通用助手", tenant_uuid=A),
                Agent(id=2, name="合同审查", tenant_uuid=B),
                Agent(id=3, name="周报助手", tenant_uuid=A),
                Thread(id=10, agent_id=1, tenant_uuid=A),
                Thread(id=20, agent_id=2, tenant_uuid=B),
                Catalog(id=1, model="minimax/MiniMax-M3"),
            ]
        )
        s.commit()
    s.expunge_all()
    return s


# ──────────────────────────────────────────────── 自动过滤


def test_no_context_on_tenant_table_raises(session: Session) -> None:
    with pytest.raises(TenantViolation) as e:
        session.scalars(sa.select(Agent)).all()
    assert e.value.kind == TenantViolation.MISSING


def test_non_tenant_table_needs_no_context(session: Session) -> None:
    assert [c.model for c in session.scalars(sa.select(Catalog))] == ["minimax/MiniMax-M3"]


def test_select_sees_only_current_tenant(session: Session) -> None:
    with TenantScope.enter(A):
        assert sorted(a.name for a in session.scalars(sa.select(Agent))) == ["周报助手", "通用助手"]
    with TenantScope.enter(B):
        assert [a.name for a in session.scalars(sa.select(Agent))] == ["合同审查"]


def test_join_and_subquery_are_filtered_too(session: Session) -> None:
    with TenantScope.enter(A):
        joined = session.execute(
            sa.select(Thread.id, Agent.name).join(Agent, Agent.id == Thread.agent_id)
        )
        assert joined.all() == [(10, "通用助手")]
        sub = sa.select(Agent.id).scalar_subquery()
        assert session.scalars(sa.select(Thread.id).where(Thread.agent_id.in_(sub))).all() == [10]
        assert session.scalar(sa.select(sa.func.count()).select_from(Agent)) == 2


def _count_forms() -> dict[str, sa.Select]:
    from sqlalchemy.orm import aliased

    ag = aliased(Agent)
    return {
        # with_loader_criteria 只管列里的 ORM 实体，下面这些它都漏（2.0.52 实测），靠 FROM 改写兜住
        "count().select_from(实体)": sa.select(sa.func.count()).select_from(Agent),
        "literal().select_from(实体)": sa.select(sa.func.count()).select_from(
            sa.select(sa.literal(1)).select_from(Agent).subquery()
        ),
        "count().select_from(别名)": sa.select(sa.func.count()).select_from(ag),
        "count() 只在 join 里出现": sa.select(sa.func.count())
        .select_from(Thread)
        .join(Agent, Agent.id == Thread.agent_id),
        "标量子查询里的 count": sa.select(
            sa.select(sa.func.count()).select_from(Agent).scalar_subquery()
        ),
        "exists 子查询": sa.select(sa.func.count())
        .select_from(Thread)
        .where(sa.exists().where(Agent.id == Thread.agent_id)),
    }


@pytest.mark.parametrize("form", list(_count_forms()))
def test_forms_with_tenant_table_only_in_from_are_filtered(session: Session, form: str) -> None:
    # 租户 A 有 2 个智能体、1 个会话（指向 A 的智能体）
    expected = {
        "count().select_from(实体)": 2,
        "literal().select_from(实体)": 2,
        "count().select_from(别名)": 2,
        "标量子查询里的 count": 2,
    }
    with TenantScope.enter(A):
        assert session.scalar(_count_forms()[form]) == expected.get(form, 1)


def test_outer_join_keeps_its_meaning(session: Session) -> None:
    """join 进来的租户表，条件加在 ON 上：左边的行不会因为右边被过滤而消失。"""
    with TenantScope.system(SystemReason.OPS):
        session.add(Thread(id=11, agent_id=2, tenant_uuid=A))  # A 的会话指向 B 的智能体（脏数据）
        session.commit()
    with TenantScope.enter(A):
        rows = session.execute(
            sa.select(Thread.id, Agent.name)
            .select_from(Thread)
            .outerjoin(Agent, Agent.id == Thread.agent_id)
            .order_by(Thread.id)
        ).all()
    assert rows == [(10, "通用助手"), (11, None)]


def test_get_by_primary_key_of_another_tenant_is_none(session: Session) -> None:
    with TenantScope.enter(A):
        assert session.get(Agent, 2) is None
        assert session.get(Agent, 1) is not None


def test_system_scope_sees_everything(session: Session) -> None:
    with TenantScope.system(SystemReason.SWEEP):
        assert session.scalar(sa.select(sa.func.count()).select_from(Agent)) == 3


def test_orm_update_and_delete_stay_in_tenant(session: Session) -> None:
    with TenantScope.enter(A):
        session.execute(sa.update(Agent).values(name="改了"))
        session.execute(sa.delete(Thread))
        session.commit()
    with TenantScope.system(SystemReason.OPS):
        names = dict(session.execute(sa.select(Agent.id, Agent.name)).all())
        threads = session.scalars(sa.select(Thread.id)).all()
    assert names == {1: "改了", 2: "合同审查", 3: "改了"}
    assert threads == [20]


def test_new_rows_get_current_tenant(session: Session) -> None:
    with TenantScope.enter(B):
        session.add(Agent(id=4, name="新的"))
        session.commit()
    with TenantScope.system(SystemReason.OPS):
        assert session.get(Agent, 4).tenant_uuid == B


def test_writing_another_tenants_row_is_refused(session: Session) -> None:
    with TenantScope.enter(A):
        session.add(Agent(id=5, name="冒名", tenant_uuid=B))
        with pytest.raises(TenantViolation) as e:
            session.flush()
    assert e.value.kind == TenantViolation.CROSS_TENANT


def test_tenant_of_existing_row_cannot_change(session: Session) -> None:
    with TenantScope.enter(A):
        agent = session.get(Agent, 1)
        agent.tenant_uuid = B
        with pytest.raises(TenantViolation):
            session.flush()


def test_system_scope_does_not_guess_tenant_for_new_rows(session: Session) -> None:
    with TenantScope.system(SystemReason.OPS):
        session.add(Agent(id=6, name="没给租户"))
        with pytest.raises(TenantViolation):
            session.flush()


# ──────────────────────────────────────────────── 上下文


def test_scope_nests_and_restores() -> None:
    assert TenantScope.current() is None
    with TenantScope.enter(A):
        with TenantScope.system(SystemReason.SWEEP):
            assert TenantScope.current() is None
            assert TenantScope.system_reason() is SystemReason.SWEEP
        assert TenantScope.require() == A
    assert TenantScope.current() is None
    with pytest.raises(TenantViolation):
        TenantScope.require()


def test_system_scope_needs_a_reason() -> None:
    with pytest.raises(TypeError), TenantScope.system("whatever"):  # type: ignore[arg-type]
        pass


async def test_child_tasks_inherit_tenant() -> None:
    async def child() -> UUID | None:
        await asyncio.sleep(0)
        return TenantScope.current()

    with TenantScope.enter(A):
        got = await asyncio.gather(asyncio.create_task(child()), asyncio.create_task(child()))
    assert got == [A, A]


async def test_sweep_fetches_in_system_scope_and_handles_per_tenant() -> None:
    seen: list[tuple[str, UUID | None]] = []

    async def fetch() -> list[tuple[str, UUID]]:
        assert TenantScope.system_reason() is SystemReason.SWEEP
        return [("r1", A), ("r2", B), ("bad", A)]

    async def handle(item: str) -> None:
        if item == "bad":
            raise RuntimeError("boom")
        seen.append((item, TenantScope.current()))

    assert await TenantSweep.each(fetch, handle) == (2, 1)
    assert seen == [("r1", A), ("r2", B)]
    assert TenantScope.current() is None


def test_names_in_shared_infrastructure_carry_tenant() -> None:
    with TenantScope.enter(A):
        assert TenantNaming.object_prefix("u1", "/files/") == f"{A}/u1/files"
        assert TenantNaming.qualified("minimax-m3") == f"{A}:minimax-m3"
        assert TenantNaming.labels() == {"atlas.tenant": str(A)}
        assert tenant_headers() == {"X-Tenant-Id": str(A)}
    with TenantScope.system(SystemReason.OPS):
        with pytest.raises(TenantViolation):
            tenant_headers()
        assert tenant_headers(B) == {"X-Tenant-Id": str(B)}


async def test_request_dependency_binds_tenant() -> None:
    async def header_tenant(request: Request) -> str | None:
        return request.headers.get("x-tenant-id")

    app = FastAPI()
    enter = tenant_dependency(header_tenant)

    @app.get("/who")
    async def who(_: UUID = Depends(enter)) -> dict[str, str]:
        return {"tenant": str(TenantScope.require())}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ok = await c.get("/who", headers={"X-Tenant-Id": str(A)})
        missing = await c.get("/who")
        bad = await c.get("/who", headers={"X-Tenant-Id": "not-a-uuid"})
    assert ok.json() == {"tenant": str(A)}
    assert missing.status_code == 401 and bad.status_code == 401


def test_purge_order_must_cover_all_tenant_tables() -> None:
    with pytest.raises(ValueError, match="漏了"):
        TenantDataPurge([Thread], base=Base)
    TenantDataPurge([Thread, Agent], base=Base)


# ──────────────────────────────────────────────── 分批删除（真库）

DB = os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(not DB.endswith("_test"), reason="只在测试库上跑（DATABASE_URL 以 _test 结尾）")
async def test_purge_deletes_in_batches_and_only_that_tenant() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    suffix = uuid4().hex[:8]

    class PgBase(DeclarativeBase):
        pass

    class P(TenantOwned, PgBase):
        __tablename__ = f"tenancy_p_{suffix}"
        id: Mapped[int] = mapped_column(primary_key=True)

    class C(TenantOwned, PgBase):
        __tablename__ = f"tenancy_c_{suffix}"
        id: Mapped[int] = mapped_column(primary_key=True)
        p_id: Mapped[int]

    engine = create_async_engine(DB)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(PgBase.metadata.create_all)
        async with AsyncSession(engine) as s:
            with TenantScope.system(SystemReason.OPS):
                s.add_all([P(id=i, tenant_uuid=A if i <= 3 else B) for i in range(1, 6)])
                s.add_all([C(id=i, p_id=1, tenant_uuid=A if i <= 4 else B) for i in range(1, 7)])
                await s.commit()

            purge = TenantDataPurge([C, P], base=PgBase)
            with pytest.raises(TenantViolation):
                await purge.batch(s, A, limit=5)

            with TenantScope.system(SystemReason.OPS):
                first = await purge.batch(s, A, limit=5)
                await s.commit()
                second = await purge.batch(s, A, limit=5)
                await s.commit()
                left = {
                    "p": (await s.execute(sa.select(P.tenant_uuid))).scalars().all(),
                    "c": (await s.execute(sa.select(C.tenant_uuid))).scalars().all(),
                }
        assert first.deleted == {C.__tablename__: 4, P.__tablename__: 1} and first.remaining
        assert second.deleted == {P.__tablename__: 2} and not second.remaining
        assert left == {"p": [B, B], "c": [B, B]}
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(PgBase.metadata.drop_all)
        await engine.dispose()
