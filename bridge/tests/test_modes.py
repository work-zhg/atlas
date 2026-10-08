"""权限模式：每一轮按 server 的要求设置，恢复会话后重新应用（doc/acp-permission-mode-design.html）。

agent 在每次新建 / 恢复会话时都会把模式重置 —— FakeAgent 照此实现，所以这里测得到
「CLI 崩溃重启后模式悄悄回到默认」这类问题。
"""

from __future__ import annotations

from typing import Any

from atlas_bridge.session.host import Phase
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import SessionHarness, until


def running_mode(h: SessionHarness, turn_id: str = "t1") -> dict[str, Any] | None:
    """这一轮第一个 running 带的 mode。"""
    for p in h.outbox.of("turn.state"):
        if p["turnId"] == turn_id and p["state"] == "running":
            return p.get("mode")
    return None


def methods(h: SessionHarness) -> list[str]:
    return [m for m, _ in h.agent.fake.received]


async def test_mode_is_set_before_the_prompt_and_reported(make: Any) -> None:
    h = make()
    await h.open()
    await h.start(mode="auto")
    assert (await h.turn_over())["outcome"]["kind"] == "completed"
    seen = methods(h)
    assert seen.index("session/set_mode") < seen.index("session/prompt")  # 先设模式再发 prompt
    assert h.agent.fake.session_mode[h.host.agent_session_id] == "auto"
    assert running_mode(h) == {
        "requested": "auto",
        "effective": "auto",
        "available": ["default", "acceptEdits", "plan", "auto"],
        "degraded": False,
    }


async def test_same_mode_is_not_set_again(make: Any) -> None:
    h = make()
    await h.open()
    await h.start("t1", mode="auto")
    await h.turn_over("t1")
    await h.start("t2", mode="auto")
    await h.turn_over("t2")
    assert methods(h).count("session/set_mode") == 1
    assert running_mode(h, "t2")["effective"] == "auto"  # type: ignore[index]


async def test_no_mode_means_no_set_mode(make: Any) -> None:
    """旧 server 不发 mode：行为与从前一致。"""
    h = make()
    await h.open()
    await h.start()
    await h.turn_over()
    assert "session/set_mode" not in methods(h)
    assert running_mode(h) is None


async def test_agent_degrading_auto_is_reported_and_the_turn_runs(make: Any) -> None:
    """模型不支持 auto 时 adapter 自行降级为 acceptEdits：如实报告，这一轮照常进行。"""
    h = make(Script(mode_fallback={"auto": "acceptEdits"}))
    await h.open()
    await h.start(mode="auto")
    assert (await h.turn_over())["outcome"]["kind"] == "completed"
    mode = running_mode(h)
    assert mode is not None
    assert (mode["requested"], mode["effective"], mode["degraded"]) == ("auto", "acceptEdits", True)


async def test_failing_to_loosen_runs_with_the_stricter_mode(make: Any) -> None:
    """要 auto 却设不上：以当前（更保守的）default 跑 —— 最多多问几次人。"""
    h = make(Script(set_mode_fails=True))
    await h.open()
    await h.start(mode="auto")
    assert (await h.turn_over())["outcome"]["kind"] == "completed"
    mode = running_mode(h)
    assert mode is not None and (mode["effective"], mode["degraded"]) == ("default", True)


async def test_failing_to_tighten_fails_the_turn_without_prompting(make: Any) -> None:
    """★ 要 plan（更保守）却设不上：不能以更宽松的权限跑，这一轮失败，prompt 不发出。"""
    h = make(Script(initial_mode="acceptEdits", set_mode_fails=True))
    await h.open()
    await h.start(mode="plan")
    ended = await h.turn_over()
    assert ended["outcome"]["kind"] == "failed"
    assert ended["outcome"]["cause"] == "mode_unavailable"
    assert "session/prompt" not in methods(h)
    assert h.outbox.turn_states("t1") == ["ended"]  # 没开始就结束：只宣告一次
    assert h.host.phase is Phase.READY  # 会话照常可用

    h.agent.fake.script = Script()  # 不再失败
    await h.start("t2", mode="plan")
    assert (await h.turn_over("t2"))["outcome"]["kind"] == "completed"


async def test_agent_without_modes(make: Any) -> None:
    h = make(Script(modes=[]))
    await h.open()
    await h.start(mode="auto")
    assert (await h.turn_over())["outcome"]["kind"] == "completed"
    assert "session/set_mode" not in methods(h)
    mode = running_mode(h)
    assert mode is not None and mode["degraded"] is True and "effective" not in mode


async def test_mode_is_reapplied_after_the_agent_restarts(make: Any) -> None:
    """★ CLI 崩溃 → bridge 重启它并恢复会话：恢复出来的会话模式被重置了，要重新应用。"""
    h = make(Script(on_prompt="crash"))
    h.agent.script_after_restart = Script()
    await h.open()
    sid = h.host.agent_session_id
    await h.start(mode="auto")
    await h.turn_over()
    await until(lambda: h.host.phase is Phase.READY and len(h.outbox.of("session.state")) == 2)

    assert h.agent.fake.session_mode[sid] == "auto"  # 不是恢复后的默认 default
    ready = h.outbox.of("session.state")[-1]
    assert ready["agent"]["mode"]["effective"] == "auto"
    seen = methods(h)
    assert seen.index("session/resume") < len(seen) - 1 - seen[::-1].index("session/set_mode")


async def test_agent_switching_mode_by_itself_is_tracked(make: Any) -> None:
    """agent 自己换了模式（如退出 plan）会发 current_mode_update：下一轮据此判断要不要再设。"""
    h = make()
    await h.open()
    await h.start("t1", mode="auto")
    await h.turn_over("t1")
    sid = h.host.agent_session_id
    await h.host.on_agent_update(
        {
            "sessionId": sid,
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": "plan"},
        }
    )
    await h.start("t2", mode="auto")
    await h.turn_over("t2")
    assert methods(h).count("session/set_mode") == 2  # 发现被改成了 plan，再设回 auto
