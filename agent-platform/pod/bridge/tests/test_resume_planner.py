"""ResumePlanner：Bridge 设计 §5.6 的表，逐行一个用例。"""

from __future__ import annotations

import pytest
from atlas_acp.v1 import AgentCaps
from atlas_bridge.agent.resume import OpenMethod, Plan, plan_open

BOTH = AgentCaps(load_session=True, resume=True)
LOAD_ONLY = AgentCaps(load_session=True)
RESUME_ONLY = AgentCaps(resume=True)
NEITHER = AgentCaps()


@pytest.mark.parametrize(
    ("caps", "want", "expected"),
    [
        (BOTH, None, Plan(OpenMethod.NEW, "none")),
        (BOTH, "none", Plan(OpenMethod.RESUME, "none")),
        (RESUME_ONLY, "none", Plan(OpenMethod.RESUME, "none")),
        (LOAD_ONLY, "none", Plan(OpenMethod.LOAD, "full")),  # 历史照样重放，server 丢弃
        (BOTH, "full", Plan(OpenMethod.LOAD, "full")),
        (LOAD_ONLY, "full", Plan(OpenMethod.LOAD, "full")),
        (RESUME_ONLY, "full", Plan(OpenMethod.RESUME, "none")),  # 无法重放，如实报告
        (NEITHER, "none", Plan(OpenMethod.NEW, "none")),
        (NEITHER, "full", Plan(OpenMethod.NEW, "none")),
    ],
)
def test_plan(caps: AgentCaps, want: str | None, expected: Plan) -> None:
    assert plan_open(caps, want) == expected  # type: ignore[arg-type]
