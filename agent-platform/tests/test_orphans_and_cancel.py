"""「父已结束时的停止」（2026-09-30 真机问题）。

★ 不碰数据库：cancel 用替身仓储与执行器。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_agent.config import get_settings
from atlas_agent.errors import Conflict
from atlas_agent.services.run import RunService


class _Runs:
    def __init__(self, status: str, children: list[UUID], **nested: list[UUID]) -> None:
        self.run = SimpleNamespace(
            id=uuid4(),
            thread_id=uuid4(),
            parent_run_id=None,
            status=status,
            error_kind=None,
            error_message=None,
            last_seq=0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            thinking_tokens=0,
            total_tokens=0,
            started_at=None,
            finished_at=None,
            created_at=datetime.now(UTC),
        )
        #: 父 run → 还没结束的直接子 run。nested 的键是子 run 的 str(id)
        self.children: dict[UUID, list[UUID]] = {self.run.id: children}
        self.children.update({UUID(k): v for k, v in nested.items()})

    async def get(self, run_id: UUID) -> Any:
        return self.run

    async def unfinished_children_of(self, run_id: UUID) -> list[UUID]:
        return self.children.get(run_id, [])


class _Executor:
    def __init__(self) -> None:
        self.cancelled: list[UUID] = []

    async def cancel(self, run_id: UUID) -> None:
        self.cancelled.append(run_id)


def _service(runs: _Runs) -> tuple[RunService, _Executor]:
    executor = _Executor()
    service = RunService(None, None, get_settings(), executor)  # type: ignore[arg-type]
    service._runs = runs  # type: ignore[assignment]
    return service, executor


async def test_stopping_a_finished_parent_still_cancels_its_running_children() -> None:
    child = uuid4()
    runs = _Runs("interrupted", [child])
    service, executor = _service(runs)
    await service.cancel(runs.run.id)
    assert executor.cancelled == [child]  # 父不再取消（已结束），只级联子


async def test_stopping_a_finished_run_without_children_is_still_a_conflict() -> None:
    runs = _Runs("succeeded", [])
    service, executor = _service(runs)
    with pytest.raises(Conflict):
        await service.cancel(runs.run.id)
    assert executor.cancelled == []


async def test_stopping_a_running_parent_cancels_it_and_its_children() -> None:
    child = uuid4()
    runs = _Runs("running", [child])
    service, executor = _service(runs)
    await service.cancel(runs.run.id)
    assert executor.cancelled == [runs.run.id, child]


async def test_stopping_cancels_the_whole_subtree() -> None:
    """★ SubAgent 可以继续派 SubAgent：停止要一层层往下走。

    只取消直接子 run 的话，挂起中的子 run 被收尾了，它派出去的孙 run 却还在跑 ——
    跑完还会去叫醒一个已经被取消的父 run。
    """
    child, grandchild, great = uuid4(), uuid4(), uuid4()
    runs = _Runs("suspended", [child], **{str(child): [grandchild], str(grandchild): [great]})
    service, executor = _service(runs)
    await service.cancel(runs.run.id)
    assert executor.cancelled == [runs.run.id, child, grandchild, great]
