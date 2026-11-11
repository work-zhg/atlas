"""bench 测试的公共设置。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _short_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """假 atlas-server 的事件流不发终止事件：收尾时不必等满 20 秒。"""
    import atlas_bench.agent.platforms.atlas as mod

    monkeypatch.setattr(mod, "_DRAIN_S", 0.3)
