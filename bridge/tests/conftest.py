from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import SessionHarness


@pytest.fixture
async def make() -> AsyncIterator[Callable[..., SessionHarness]]:
    """构造 SessionHarness；测试结束时统一关掉进程内 agent。"""
    made: list[SessionHarness] = []

    def factory(script: Script | None = None, **host_kw: Any) -> SessionHarness:
        made.append(SessionHarness(script, **host_kw))
        return made[-1]

    yield factory
    for h in made:
        await h.close()
