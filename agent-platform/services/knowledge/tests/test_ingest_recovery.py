"""入库任务中断后的恢复：入库锁（短 TTL + 续租、owner 校验）与巡检 recover_stalled。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from atlas_knowledge.db.models import Document
from atlas_knowledge.db.session import get_sessionmaker
from atlas_knowledge.indexing import runner as runner_mod
from atlas_knowledge.indexing.runner import IndexingRunner, lock_key, recover_stalled
from conftest import run_indexing
from sqlalchemy import select

ALICE = {"X-Uid": "alice"}
FAQ = "退款政策：购买后七天内可以申请无理由退款。\n\n发票说明：电子发票在订单完成后自动开具。"


async def _queued_doc(client: Any, rt: Any) -> str:
    """建库、上传一份文档，投递记录清掉（模拟「任务已被 worker 领走」）。"""
    r = await client.post(
        "/v1/datasets", json={"name": "恢复", "embedding_model": "fake-embedding"}, headers=ALICE
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/v1/datasets/{r.json()['id']}/documents",
        files=[("files", ("faq.txt", FAQ.encode(), "text/plain"))],
        headers=ALICE,
    )
    assert r.status_code == 201, r.text
    rt.dispatcher.queued.clear()
    return str(r.json()["documents"][0]["id"])


async def _status(doc_uuid: str) -> str:
    async with get_sessionmaker()() as s:
        doc = await s.scalar(select(Document).where(Document.uuid == UUID(doc_uuid)))
        assert doc is not None
        return doc.indexing_status


async def _set_status(doc_uuid: str, status: str) -> None:
    async with get_sessionmaker()() as s:
        doc = await s.scalar(select(Document).where(Document.uuid == UUID(doc_uuid)))
        assert doc is not None
        doc.indexing_status = status
        await s.commit()


async def test_dead_worker_is_recovered_by_sweep(client: Any, rt: Any) -> None:
    doc = await _queued_doc(client, rt)
    # worker 跑到一半被杀：状态停在 indexing，锁还没过期
    await _set_status(doc, "indexing")
    await rt.redis.set(lock_key(doc), "dead-worker", px=60_000)

    # 子进程被杀后 Celery 立刻重投的那次：锁还在，跳过，不动状态
    await IndexingRunner(rt, get_sessionmaker()).run(doc)
    assert await _status(doc) == "indexing"
    # 锁没过期时巡检不投
    assert await recover_stalled(rt, get_sessionmaker()) == []

    # 一个 TTL 后锁自然过期 → 巡检重新投递 → 续跑完成
    await rt.redis.delete(lock_key(doc))
    assert await recover_stalled(rt, get_sessionmaker()) == [doc]
    assert rt.dispatcher.queued == [doc]
    await run_indexing(rt)
    assert await _status(doc) == "completed"
    assert not await rt.redis.exists(lock_key(doc))


async def test_api_lifespan_runs_sweep(client: Any, rt: Any) -> None:
    from atlas_knowledge.api.app import create_app

    doc = await _queued_doc(client, rt)
    await _set_status(doc, "parsing")
    rt.settings = rt.settings.model_copy(update={"ingest_sweep_interval_s": 0.05})
    app = create_app(runtime=rt)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if rt.dispatcher.queued:
                break
            await asyncio.sleep(0.02)
    assert rt.dispatcher.queued[0] == doc


@pytest.mark.parametrize("status", ["waiting", "paused", "error", "completed"])
async def test_sweep_ignores_settled_and_queued(client: Any, rt: Any, status: str) -> None:
    doc = await _queued_doc(client, rt)
    await _set_status(doc, status)
    assert await recover_stalled(rt, get_sessionmaker()) == []
    assert rt.dispatcher.queued == []


async def test_lost_lock_interrupts_without_marking_error(
    client: Any, rt: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = await _queued_doc(client, rt)
    monkeypatch.setattr(runner_mod, "LOCK_TTL_S", 0.3)
    started = asyncio.Event()

    async def slow(self: IndexingRunner, doc_uuid: UUID) -> None:
        await self._set(doc_uuid, indexing_status="indexing")
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(IndexingRunner, "_run", slow)
    task = asyncio.create_task(IndexingRunner(rt, get_sessionmaker()).run(doc))
    await started.wait()
    # 锁被别人占了（比如 Redis 抖动超过一个 TTL 后被接手）：看门狗发现后中断本任务
    await rt.redis.set(lock_key(doc), "someone-else", px=60_000)
    await asyncio.wait_for(task, timeout=5)
    # 不标失败（接手的任务还在跑），也不删别人的锁
    assert await _status(doc) == "indexing"
    assert await rt.redis.get(lock_key(doc)) == b"someone-else"
