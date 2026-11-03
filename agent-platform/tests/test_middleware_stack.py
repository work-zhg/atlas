"""中间件顺序只在一处定（atlas_engine/kernel/stack.py，code-native-runtime-turn.html）。"""

from __future__ import annotations

from atlas_engine.kernel import MiddlewareStack, Slot


def test_order_comes_from_slots_not_from_who_added_first() -> None:
    """各领域按槽位交中间件；交进来的先后不影响槽位之间的顺序。"""
    stack = MiddlewareStack()
    stack.add(Slot.LIMITS, "steps")
    stack.add(Slot.TOOLS, "sandbox")
    stack.add(Slot.SUSPEND, "suspension")
    stack.add(Slot.CONTEXT, "compaction")
    stack.add(Slot.PERMISSION, "permission")
    stack.add(Slot.ASK, "ask")
    stack.add(Slot.SUBAGENTS, "task")
    assert stack.ordered() == [
        "suspension",
        "ask",
        "permission",
        "compaction",
        "sandbox",
        "steps",
        "task",
    ]


def test_same_slot_keeps_the_order_it_was_added_in() -> None:
    """★ 同一槽位按交进来的先后：内核先放的文件中间件要包在调用方的沙箱外面，
    大结果外置才接得住 execute 的大输出。merge 进来的排在已有的后面。"""
    caller = MiddlewareStack().add(Slot.TOOLS, "sandbox").add(Slot.SUSPEND, "suspension")
    kernel = MiddlewareStack().add(Slot.TOOLS, "skills").add(Slot.TOOLS, "filesystem")
    kernel.merge(caller)
    assert kernel.ordered() == ["suspension", "skills", "filesystem", "sandbox"]


def test_limits_wrap_the_subagent_tool() -> None:
    """★ 限额在子智能体外层：task 由子智能体中间件拦下、不往里调用，限额排在里面的话
    并发上限和连续失败熔断就管不到 task。"""
    assert Slot.LIMITS < Slot.SUBAGENTS
